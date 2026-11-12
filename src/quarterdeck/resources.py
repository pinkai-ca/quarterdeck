"""Target-owned framework resources, shared by packaging and explicit upgrades only."""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import tomllib

from .ship import ShipError

PREFIX = PurePosixPath('share/quarterdeck')
FALLBACK = ('resources: unsupported legacy copy step; copied only target standing-orders and drydock '
            'where present (extra templates may remain)')
NO_LAWS = 'resources: laws/cards unavailable at this target commit; --help proof only, seat briefs may be unusable'
SOURCE_LIMIT = 1024 * 1024


class UnsupportedLegacy(ValueError):
    """An old target has no recognized declarative resource step."""


def relative_file(value: str) -> Path:
    """Declarations name individual framework files, without expansion or private paths."""
    path = PurePosixPath(value)
    if (not value or not path.parts or path.is_absolute() or str(path) != value
            or any(part in ('.', '..', 'runtime', '.git', '__pycache__') or part.startswith('.')
                   or any(char in part for char in '*?[]\\') for part in path.parts)
            or path.parts[0] not in ('standing-orders', 'ships')):
        raise ValueError('resource must name an explicit framework file')
    return Path(*path.parts)


def plain_file(root: Path, relative: Path) -> None:
    for parent in (relative, *relative.parents):
        if (root / parent).is_symlink():
            raise ValueError('resource path contains a link')
    if not (root / relative).is_file():
        raise ValueError('declared resource file is missing')


def declared(source: Path, table: dict) -> tuple[Path, ...]:
    files = []
    if not isinstance(table, dict) or not table:
        raise ValueError('resource declaration must be a nonempty table')
    for destination, names in table.items():
        if not isinstance(names, list) or not names:
            raise ValueError('resource declaration must list individual files')
        for name in names:
            if not isinstance(name, str):
                raise ValueError('resource declaration must list file names')
            path = relative_file(name)
            if destination != str(PREFIX / PurePosixPath(path.parent.as_posix())):
                raise ValueError('resource destination must preserve its source directory')
            plain_file(source, path)
            files.append(path)
    if len(set(files)) != len(files):
        raise ValueError('duplicate resource file')
    return tuple(sorted(files))


def shape(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def statement(text: str) -> ast.stmt:
    return ast.parse(text).body[0]


def legacy(source: Path) -> tuple[Path, ...]:
    """Read one literal historical copy loop, never import or execute target code."""
    with (source / 'src/quarterdeck/install.py').open('rb') as stream:
        data = stream.read(SOURCE_LIMIT + 1)
    if len(data) > SOURCE_LIMIT:
        raise UnsupportedLegacy('legacy source exceeds bound')
    tree = ast.parse(data)
    if sum(1 for _ in ast.walk(tree)) > 20000:
        raise UnsupportedLegacy('legacy syntax exceeds bound')
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'upgrade']
    if len(functions) != 1:
        raise UnsupportedLegacy('no unique upgrade function')
    function = functions[0]
    copies = [node for node in ast.walk(function) if isinstance(node, ast.Call)
              and shape(node.func) == shape(ast.parse('shutil.copytree', mode='eval').body)]
    loops = [node for node in function.body if isinstance(node, ast.For)
             and isinstance(node.target, ast.Name) and node.target.id == 'name']
    share = statement('share = venv / "share" / "quarterdeck"')
    if len(copies) != 1 or sum(shape(node) == shape(share) for node in function.body) != 1:
        raise UnsupportedLegacy('unrecognized resource writer')
    remove = statement('shutil.rmtree(share / name, ignore_errors=True)')
    copy = statement('shutil.copytree(staging / name, share / name)')
    matches = []
    for loop in loops:
        if (loop.orelse or not isinstance(loop.iter, ast.Tuple) or not loop.iter.elts
                or any(not isinstance(item, ast.Constant) or not isinstance(item.value, str)
                       for item in loop.iter.elts)):
            continue
        roots = [item.value for item in loop.iter.elts]
        body = loop.body
        if len(body) not in (2, 3) or shape(body[0]) != shape(remove) or shape(body[-1]) != shape(copy):
            continue
        optional = ()
        if len(body) == 3:
            skip = body[1]
            if (not isinstance(skip, ast.If) or skip.orelse or len(skip.body) != 1
                    or not isinstance(skip.body[0], ast.Continue)
                    or not isinstance(skip.test, ast.BoolOp) or not isinstance(skip.test.op, ast.And)
                    or len(skip.test.values) != 2):
                continue
            selection, absent = skip.test.values
            if shape(absent) != shape(ast.parse('not (staging / name).exists()', mode='eval').body):
                continue
            if (not isinstance(selection, ast.Compare) or len(selection.ops) != 1
                    or shape(selection.left) != shape(ast.Name(id='name', ctx=ast.Load()))):
                continue
            right = selection.comparators[0]
            if isinstance(selection.ops[0], ast.Eq) and isinstance(right, ast.Constant) and isinstance(right.value, str):
                optional = (right.value,)
            elif (isinstance(selection.ops[0], ast.In) and isinstance(right, ast.Tuple)
                  and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in right.elts)):
                optional = tuple(item.value for item in right.elts)
            else:
                continue
            if not set(optional) <= set(roots):
                continue
        matches.append((roots, optional))
    if len(matches) != 1:
        raise UnsupportedLegacy('no unique literal resource loop')
    roots, optional = matches[0]
    if len(set(roots)) != len(roots):
        raise UnsupportedLegacy('duplicate legacy roots')
    files = []
    for name in roots:
        root = relative_file(name)
        if name in optional and not (source / root).exists():
            continue
        if not (source / root).is_dir() or (source / root).is_symlink():
            raise UnsupportedLegacy('legacy root unavailable')
        for path in sorted((source / root).rglob('*')):
            if path.is_symlink():
                raise UnsupportedLegacy('legacy resource link')
            if path.is_file():
                relative = relative_file(path.relative_to(source).as_posix())
                plain_file(source, relative)
                files.append(relative)
    if len(set(files)) != len(files) or not files:
        raise UnsupportedLegacy('ambiguous legacy files')
    return tuple(sorted(files))


def plan(source: Path) -> tuple[Path, ...] | None:
    """None means unsupported legacy: use only the explicitly authorized baseline fallback."""
    try:
        config = tomllib.loads((source / 'pyproject.toml').read_text())
        table = config.get('tool', {}).get('setuptools', {}).get('data-files')
        if table is not None:
            return declared(source, table)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ShipError(f'invalid target resource declaration: {error}') from None
    try:
        return legacy(source)
    except (OSError, ValueError, SyntaxError, RecursionError, TypeError):
        return None


def share_path(share: Path) -> None:
    for parent in (share, *share.parents):
        if parent.is_symlink():
            raise ShipError('framework share path contains a link')


def fallback(source: Path, share: Path) -> bool:
    """The two authorized historical base roots, independent of packaging authority."""
    share_path(share)
    for name in ('standing-orders', 'ships/drydock'):
        root = source / name
        if not root.exists():
            continue
        if root.is_symlink() or any(path.is_symlink() for path in root.rglob('*')):
            raise ShipError('fallback resource path contains a link')
        destination = share / name
        share_path(destination)
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root, destination)
    return (source / 'standing-orders').exists()


def synchronize(source: Path, share: Path, files: tuple[Path, ...]) -> None:
    """Build the target share completely before replacing the managed tree."""
    share_path(share)
    share.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.quarterdeck-resources-', dir=share.parent) as temporary:
        ready = Path(temporary) / 'ready'
        ready.mkdir()
        for relative in files:
            try:
                plain_file(source, relative)
            except ValueError as error:
                raise ShipError(f'invalid target resource: {error}') from None
            destination = ready / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, destination)
        if share.exists():
            shutil.rmtree(share)
        ready.rename(share)
