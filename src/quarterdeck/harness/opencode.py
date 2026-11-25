"""OpenCode: a SQLite database per launch, ``data-<n>/opencode/opencode.db`` in the seat home.

OpenCode keeps every session of a data directory in one database, so the seat home gives each
launch a data directory of its own, named by ``XDG_DATA_HOME``, and the record is the database
written there; a resumed launch names the directory holding the session it continues. The
``session`` table dates the record; the ``message`` table holds the turn structure, a user message
and the assistant messages that answer it, each with its model, tokens, cost and completion time;
the ``part`` table holds the text, the tool calls with their timing and exit, and the step
boundaries. The database is read from a snapshot of its bytes and its write-ahead log, never
opened in place (law: one-folder). Proven on 1.19.5 against a local vLLM endpoint.
"""

from __future__ import annotations

from .. import seat_files
from .capabilities import DefaultMetadata, EffortCapability, SuggestedValue

import copy
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path

from .. import durable, endpoint
from ..pricing import Price
from .endpoint import EndpointHarness
from ..ship import Pool, SeatSpec, ShipError
from .base import (Box, ship_template, trimmed, Control, Credential, Interruption, Keys, Launch, Screen, Tokens,
                   merged)
from .types import _cells

from .opencode_record import (DATA_PREFIX, RECORD, _Rows, _launch_number, data_dir_of, first_timestamp,
                              inputs, read, records, session_id, state)

CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "autoupdate": False,  # versions change when the operator says, and the status table says so
    "share": "disabled",  # a seat's conversation is the ship's, never a public link
}


# A Z.ai key launch: the generated key field is the one place native substitution may run.
_ZAI_KEY_FIELD = ('options', 'apiKey')
_ZAI_KEY_MARKER = f'{{env:{endpoint.KEY_VARIABLE}}}'
_ZAI_SUBSTITUTION = re.compile(r'\{(?:env|file):', re.IGNORECASE)


def zai_route_matches(config, pool) -> bool:
    providers = config.get('provider') if isinstance(config, dict) else None
    provider = providers.get(pool.name) if isinstance(providers, dict) else None
    if not isinstance(provider, dict) or provider.get('npm') != '@ai-sdk/openai-compatible':
        return False
    options, models = provider.get('options'), provider.get('models')
    if (not isinstance(options, dict) or not isinstance(models, dict)
            or options.get('baseURL') != pool.endpoint or options.get('apiKey') != _ZAI_KEY_MARKER
            or {'headers', 'fetch'} & options.keys()):
        return False
    routing = {'api', 'apiKey', 'baseURL', 'baseUrl', 'fetch', 'headers', 'id', 'provider', 'npm'}
    return all(isinstance(model, dict) and isinstance(model.get('options', {}), dict)
               and not routing & model.keys() and not routing & model.get('options', {}).keys()
               for model in models.values())


def _zai_substitution(value, key_field: tuple) -> bool:
    """Native substitution scans the JSON text, including keys, before parsing it."""
    if isinstance(value, dict):
        return any(_ZAI_SUBSTITUTION.search(key) or
                   _zai_substitution(child, key_field[1:] if key_field and key == key_field[0] else ())
                   for key, child in value.items()
                   if not (key_field == (key,) and child == _ZAI_KEY_MARKER))
    if isinstance(value, list):
        return any(_zai_substitution(child, ()) for child in value)
    return isinstance(value, str) and _ZAI_SUBSTITUTION.search(value) is not None


def require_zai_config(config, pool, model) -> None:
    """Apply the keyed route constraints to the merged native configuration."""
    if not zai_route_matches(config, pool):
        raise ShipError('Z.ai OpenCode settings contain authentication or routing overrides')
    providers = config['provider']
    if set(providers) != {pool.name} or config.get('enabled_providers') != [pool.name]:
        raise ShipError('Z.ai OpenCode requires only the selected pool provider')
    if set(providers[pool.name]['models']) != {model}:
        raise ShipError('Z.ai OpenCode requires only the selected model')
    qualified = f'{pool.name}/{model}'
    if config.get('model') != qualified or config.get('small_model') != qualified:
        raise ShipError('Z.ai OpenCode requires the selected model for both model settings')
    for group in ('agent', 'mode'):
        scopes = config.get(group, {})
        if not isinstance(scopes, dict) or any(
                not isinstance(scope, dict) or any(scope.get(key, qualified) != qualified for key in ('model', 'small_model'))
                for scope in scopes.values()):
            raise ShipError('Z.ai OpenCode agents and modes must use the selected model')
    if (config.get('share') != 'disabled' or config.get('autoshare', False) is not False
            or config.get('autoupdate') is not False):
        raise ShipError('Z.ai OpenCode requires disabled sharing and updates')
    if any(config.get(option, empty) != empty for option, empty in (('mcp', {}), ('plugin', []), ('enterprise', {}))):
        raise ShipError('Z.ai OpenCode requires empty MCP, plugin and enterprise settings')
    instructions = config.get('instructions')
    if isinstance(instructions, list) and any(
            isinstance(entry, str) and entry.lstrip().lower().startswith(('http://', 'https://'))
            for entry in instructions):
        raise ShipError('Z.ai OpenCode instructions must use local paths')
    if _zai_substitution(config, ('provider', pool.name, *_ZAI_KEY_FIELD)):
        raise ShipError('Z.ai OpenCode settings contain a substitution marker outside the generated key field')


def configure_google_key(seat: SeatSpec, provider: dict, model: dict, model_id: str) -> None:
    """Use OpenCode's bundled Google SDK and keep the declaration's model selector."""
    native = '@ai-sdk/google'
    base = endpoint.GOOGLE_GENERATIVE_BASE
    addresses = (base, base + '/', endpoint.GOOGLE_OPENAI_BASE, endpoint.GOOGLE_OPENAI_BASE + '/')
    upstream_id = model_id.removeprefix('models/')
    options = provider.get('options')
    if (provider.get('npm') not in (native, '@ai-sdk/openai-compatible')
            or provider.get('api', base) not in addresses
            or not isinstance(options, dict) or options.get('baseURL') not in addresses
            or options.get('apiKey') != f'{{env:{endpoint.KEY_VARIABLE}}}'
            or {'headers', 'fetch'} & options.keys()
            or {'headers', 'provider'} & model.keys()
            or model.get('id', upstream_id) not in (model_id, upstream_id)):
        raise ShipError(f"seat {seat.name}: OpenCode on Google's native API needs the declared Google address and key; "
                        "remove routing and authentication overrides from its opencode.json provider and selected model")
    provider['npm'] = native
    options['baseURL'] = base
    model['id'] = upstream_id


# -- the adapter --------------------------------------------------------------------------------

BAR_TEXT = re.compile(r"^\s*┃  (.*)$")
BAR_BARE = re.compile(r"^\s*┃\s*$")
STATUS = re.compile(r"^\s*┃  (\S+) (\S+) · (.+?)\s*$")  # "Build auto · Qwen3.8 27B local-a": the agent, the permission mode, the model and pool as shown
RULE = re.compile(r"^\s*╹▀{3,}")
PLACEHOLDER = re.compile(r'^Ask anything(?:…|\.\.\.) "[^"]*"\s*$')  # a fresh box's hint, with a question that varies
WORKING = re.compile(r"\besc interrupt\b")
QUEUED_TAG = re.compile(r"^\s*┃\s+QUEUED\s*$")
COMPACT = re.compile(r"ctrl\+p cmd\s*$", re.M)
SIDEBAR_FRAGMENT = re.compile(r" {4,}(\S+) *$")
SIDEBAR_VERSION = re.compile(r" {4,}(• OpenCode \d+\.\d+\.\d+(?:[-+][\w.-]+)?) *$")
SIDEBAR_FOLDER = re.compile(r"~?/(?:[^/\s]+/)*[^/\s]+/?")
MODAL_HEADING = re.compile(r"^ +(?P<title>[^\s┃].*?) {2,}(?P<hint>esc(?:/enter)?) *$")


def _bar(line: str) -> str | None:
    """The text of one box row; the empty string for a bare bar; None for a line that is not a bar."""
    if BAR_BARE.match(line):
        return ""
    found = BAR_TEXT.match(line)
    return found.group(1) if found else None


def _wrapped_folder_pad(lines: list[str], rule_at: int) -> bool:
    """Prove the native two-line folder in the session sidebar, beside the final pad and status."""
    rule = lines[rule_at].rstrip(" ")
    if not RULE.fullmatch(rule) or rule_at + 1 >= len(lines):
        return False
    pad, status, footer = lines[rule_at - 2], lines[rule_at - 1], lines[rule_at + 1]
    first = SIDEBAR_FRAGMENT.search(pad)
    last = SIDEBAR_FRAGMENT.search(status)
    version = SIDEBAR_VERSION.search(footer)
    if first is None or last is None or version is None:
        return False
    sidebar = len(rule) + 4  # the full composer rule ends four cells before the sidebar
    if any(_cells(row[:part.start(1)]) != sidebar
           for row, part in ((pad, first), (status, last), (footer, version))):
        return False
    pane_columns = sidebar + 40  # the verified session sidebar is forty cells wide
    for row in lines[:rule_at - 2]:
        heading = MODAL_HEADING.fullmatch(row)
        if heading is None:
            continue
        left = _cells(row[:heading.start("title")])
        right = _cells(row[:heading.end("hint")])
        # Native selectors, prompts and info dialogs center this heading pair in the whole pane.
        if left is None or right is None or left + right == pane_columns + pane_columns % 2:
            return False
    border = rule[:rule.index("╹")] + "┃"
    left_status = status[:last.start(1)].rstrip(" ")
    return (pad[:first.start(1)].rstrip(" ") == border and left_status.startswith(border + "  ")
            and STATUS.fullmatch(left_status) is not None
            and first.group(1).startswith(("/", "~/"))
            and SIDEBAR_FOLDER.fullmatch(first.group(1) + last.group(1)) is not None)


def question_stays_denied(raw: str | None) -> bool:
    """V1 session rules follow agent rules. Unknown saved overrides cannot certify exclusion."""
    if raw is None:
        return True
    try:
        rules = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(rules, list):
        return False
    last = {'action': 'deny', 'pattern': '*'}  # the generated agent's final policy
    for rule in rules:
        if (not isinstance(rule, dict) or not all(isinstance(rule.get(key), str) for key in ('permission', 'pattern', 'action'))
                or rule['action'] not in ('allow', 'ask', 'deny')):
            return False
        # Native Wildcard.match: normalized slashes, * and ?, optional trailing arguments.
        pattern = re.escape(rule['permission'].replace('\\', '/')).replace(r'\ ', ' ').replace(r'\*', '.*').replace(r'\?', '.')
        if pattern.endswith(' .*'):
            pattern = pattern[:-3] + '( .*)?'
        if re.fullmatch(pattern, 'question', re.S):
            last = rule
    return last['action'] == 'deny' and last['pattern'] == '*'


def guard_question_configuration(home: Path) -> None:
    """Native home agent/mode files load after JSON rules, even with project discovery off."""
    def metadata(path):
        try:
            return path.lstat()
        except FileNotFoundError:
            return None

    def refuse(path):
        raise ShipError(f'opencode: unmanaged native configuration at {path.relative_to(home)}; '
                        'move it to ship.toml settings and remove this unmanaged scope before launch, or use a human session')

    roots = (home / '.opencode', home / 'config/opencode')
    for path in (home, home / 'config', *roots):
        found = metadata(path)
        if found is not None and not stat.S_ISDIR(found.st_mode):
            refuse(path)  # do not follow a native configuration symlink
    for root in roots:
        files = ('opencode.json', 'opencode.jsonc') if root == roots[0] else ('config.json', 'opencode.jsonc', 'config')
        for name in files:
            if metadata(root / name) is not None:
                refuse(root / name)
        for name in ('agent', 'agents', 'mode', 'modes'):
            path = root / name
            found = metadata(path)
            if found is not None and (not stat.S_ISDIR(found.st_mode) or next(path.iterdir(), None) is not None):
                refuse(path)


class OpenCodeHarness(EndpointHarness):
    """OpenCode, proven on 1.19.5. Runs on an endpoint pool; the model changes only at launch."""

    box_geometry_known = True  # box_columns below declares the established text area

    efforts = EffortCapability("none", measured_versions=("1.19.5",), complete=True)
    defaults = DefaultMetadata(model_default=False, effort=SuggestedValue(None, "program-default"))
    effort_selectors = ()  # native variants are not the seat effort interface
    unclassified_default_overrides = False
    selection_templates = ()  # model-default is refused; there is no native seat effort selector

    name = "opencode"
    display_name = 'OpenCode'
    program = "opencode"
    home_variable = "XDG_DATA_HOME"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape Escape")  # Escape once opens nothing; twice, quickly, interrupts
    human_tools = ('question',)
    human_tool_policy = "The native question tool is denied for every configured agent."

    def accepts(self, pool: Pool) -> str | None:
        if pool.provider == 'google' and pool.paid_by_key and not endpoint.google_openai(pool.endpoint):
            return (f'pool {pool.name}: OpenCode needs endpoint = "{endpoint.GOOGLE_OPENAI_BASE}" '
                    'for a Google API key')
        return super().accepts(pool)

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        if reason := self.accepts(pool):
            raise ShipError(f"seat {seat.name}: {reason}")
        self.check_defaults(seat, ship_home)
        zai = pool.provider == 'zai' and pool.paid_by_key
        if not seat.human_session or zai:
            guard_question_configuration(home)
        seat_files.make_folder(home)  # never through a link its jail planted
        runtime = home.parents[2]  # runtime/seats/<seat>/home
        choice, secret = self.endpoint_home(home, seat, pool)
        provider = {
            "npm": "@ai-sdk/openai-compatible",
            "name": pool.name,
            "options": {"baseURL": pool.endpoint, "apiKey": f"{{env:{endpoint.KEY_VARIABLE}}}" if secret else "local"},
            "models": {choice.model: {"name": choice.model, "tool_call": True, "limit": {"context": choice.context, "output": choice.output}}},
        }
        config = {**CONFIG, "model": f"{pool.name}/{choice.model}", "provider": {pool.name: provider},
                  "instructions": [str(runtime / "brief" / f"{seat.name}.md")]}  # the brief, and no instruction file of the user's
        if zai:
            config.update(small_model=config['model'], enabled_providers=[pool.name])
        layered = ship_template(ship_home, "opencode.json")
        config = copy.deepcopy(merged(merged(config, layered), seat.settings))
        if zai:  # before the crew's own edits read the merged agents and modes
            require_zai_config(config, pool, choice.model)
        if not seat.human_session:
            # V1 evaluates the last matching rule. Agent rules follow root rules.
            modes = config.get('mode', {})
            scopes = [(config, True), *((scope, name not in modes) for name, scope in config.get('agent', {}).items()),
                      *((scope, True) for scope in modes.values())]
            for scope, final in scopes:
                rules = scope.get('permission', {})
                rules = {'*': rules} if isinstance(rules, str) else dict(rules)
                rules.pop('question', None)
                # A mode overlay must insert the final key, not update an earlier
                # agent key before newly merged wildcard allows.
                scope['permission'] = {**rules, **({'question': 'deny'} if final else {})}
                if isinstance(scope.get('tools'), dict) and 'question' in scope['tools']:
                    scope['tools'].pop('question')  # native Object.assign would insert this key before wildcard rules
        price = Price.parse(pool.price) if pool.price is not None else None
        provider = config["provider"][pool.name]
        model = provider["models"][choice.model]
        if pool.provider == 'google' and pool.paid_by_key:
            configure_google_key(seat, provider, model, choice.model)
            config.setdefault('small_model', f'{pool.name}/{choice.model}')
        from .opencode_thinking import prepare
        prepare(config, pool, choice.model, seat.human_session)
        model.pop("cost", None)
        if price:
            model["cost"] = {"input": price.input, "output": price.output,
                             "cache_read": price.cache_read, "cache_write": price.cache_write}
        if zai:  # and again on the file as it will be written, after every addition
            require_zai_config(config, pool, choice.model)
        folder = home / "config" / "opencode"
        seat_files.make_folder(folder)  # never through a link its jail planted
        durable.replace_file(folder / "opencode.json", json.dumps(config, indent=2) + "\n")
        return self.endpoint_ready(home, choice, secret, price)

    @staticmethod
    def _working_folder(seat: SeatSpec, home: Path) -> Path | None:
        # OpenCode uses endpoint pools, which are jailed unless explicitly opted out.
        if not seat.human_session and seat.role == 'hand' and seat.sandbox is not False:
            return seat.cwd if seat.cwd is not None else home.parent / 'work'
        return None

    def preflight_resume(self, seat: SeatSpec, home: Path, resume_id: Callable[[], str | None]) -> None:
        folder = self._working_folder(seat, home)
        if folder is None:
            return
        resume = resume_id()
        if resume is None:
            raise ShipError(f'opencode: {seat.name} has no proved session directory; '
                            f'qd fresh {seat.name} starts a scoped session and retains the old record')
        self._data_dir(home, resume, seat.name, working_folder=folder)

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        zai = pool.provider == 'zai' and pool.paid_by_key
        if not seat.human_session or zai:
            guard_question_configuration(home)
        folder = self._working_folder(seat, home)
        choice = endpoint.Choice.read(home)
        if zai:
            if choice.model != seat.model:
                raise ShipError('Z.ai OpenCode model choice differs from the declaration')
            try:
                config = json.loads(seat_files.read_text(home / 'config/opencode/opencode.json') or '')
            except (OSError, ValueError, RecursionError):
                raise ShipError('Z.ai OpenCode launch configuration is unreadable') from None
            require_zai_config(config, pool, choice.model)
        data = self._data_dir(home, resume, seat.name, working_folder=folder)
        if resume and not seat.human_session:
            rows = _Rows(data / RECORD, resume_metadata=True)
            if not all(question_stays_denied(raw) for raw in rows.permissions.values()):
                raise ShipError(f'{seat.name}: saved native session permissions may re-enable question; '
                                'remove that override before restart, or use qd fresh for a new native session')
        argv = [self.program, "--auto", "--model", f"{pool.name}/{choice.model}"]  # --auto: no approval prompt (law: can-post)
        if resume:
            argv += ["--session", resume]
        if first:
            argv += ["--prompt", first]
        env = {"XDG_CONFIG_HOME": str(home / "config"), self.home_variable: str(data), "XDG_CACHE_HOME": str(home / "cache"),
               "XDG_STATE_HOME": str(home / "state"), "OPENCODE_DISABLE_AUTOUPDATE": "1", **endpoint.key(pool)}
        if not seat.human_session or zai:
            # V1 loads project/config-env rules after its global config.
            env.update(HOME=str(home), OPENCODE_DISABLE_PROJECT_CONFIG='1',
                       OPENCODE_CONFIG=str(home / 'config/opencode/opencode.json'),
                       OPENCODE_CONFIG_DIR=str(home / 'config/opencode'),
                       OPENCODE_CONFIG_CONTENT='{}')
        if not seat.human_session:
            env['OPENCODE_PERMISSION'] = '{"question":"deny"}'
        if zai:
            # The compatible SDK is bundled. The catalog, external plugins and the
            # unconditional config-directory dependency installer each have a separate switch.
            env.update(OPENCODE_DISABLE_MODELS_FETCH='1', OPENCODE_PURE='1', npm_config_offline='true',
                       OPENCODE_DISABLE_LSP_DOWNLOAD='1', OPENCODE_DISABLE_DEFAULT_PLUGINS='1')
        if folder is not None:
            try:
                if seat_files.under_seat(folder):
                    seat_files.make_folder(folder)  # in the seat folder: never through a link its jail planted
                else:
                    folder.mkdir()  # a declared folder elsewhere: never its parents (law: one-folder)
            except FileExistsError:
                if not stat.S_ISDIR(folder.lstat().st_mode):
                    raise ShipError(f'opencode: {folder} must be a seat-owned working directory') from None
            except OSError:
                raise ShipError(f'opencode: {folder} must be a seat-owned working directory') from None
            # The native project argument starts its finder in the declared workspace.
            argv.insert(1, str(folder))
            if seat.cwd is not None:
                env['QD_WORKSPACE'] = str(seat.cwd)
        from .opencode_thinking import launch_evidence
        recovery = launch_evidence(home, seat, pool, choice, env)
        return Launch(tuple(argv), env, choice.price, recovery)

    def thinking_recovery(self, home, evidence, version, seat, pool):
        from .opencode_thinking import client
        return client(home, evidence, version, seat, pool)

    @staticmethod
    def _data_dir(home: Path, resume: str | None, seat_name: str, *, working_folder: Path | None = None) -> Path:
        """A fresh launch gets the next numbered data directory; a resumed one, the directory holding its session."""
        existing = records(home)
        if resume:
            selected = None
            for record in reversed(existing):
                try:
                    rows = _Rows(record, resume_metadata=working_folder is not None)
                except OSError:
                    continue
                matches = sum(row[0] == resume for row in rows.sessions)
                if not matches:
                    continue
                from ..record_location import retired
                if retired(record):
                    raise ShipError(f"{seat_name}: conversation is retired; use qd fresh {seat_name}")
                if working_folder is None:
                    return data_dir_of(record)
                if selected is not None or matches != 1:
                    raise ShipError(f'opencode: {seat_name} session has ambiguous directory evidence; '
                                    f'qd fresh {seat_name} starts a scoped session and retains the old record')
                selected = (data_dir_of(record), rows.directories.get(resume))
            if selected is not None:
                data, directory = selected
                # Native search follows the session's saved directory after resume.
                if directory != str(working_folder):
                    raise ShipError(f'opencode: {seat_name} session directory does not prove its seat-owned working folder: '
                                    f'recorded {directory!r}, expected {str(working_folder)!r}; '
                                    f'qd fresh {seat_name} starts a scoped session and retains the old record')
                return data
            raise ShipError(f"{seat_name}: no record in {home} holds the session {resume}; qd fresh {seat_name} starts a new one")
        number = max((_launch_number(r) for r in existing), default=0) + 1
        return home / f"{DATA_PREFIX}{number:03d}"

    inputs = staticmethod(inputs)
    retirement_process_names = ('opencode', 'bun', 'node')

    def launch_record(self, home: Path, launch) -> Path | None:
        """The record the launch writes: its data folder's database, named by the trusted launch record before the
        program has made it."""
        from .opencode_record import launched_data
        data = launched_data(home, launch)
        return None if data is None else data / RECORD

    def stamp_extra(self, record: Path):
        """What besides its bytes decides a record's reading: whether a later launch of its home exists (ledger)."""
        from .opencode_record import later_launch
        return ['later-launch', later_launch(record)]

    def retirement_bundle(self, home: Path, record: Path, kept: Path):
        if not record.is_relative_to(home):
            return None
        relative = record.relative_to(home)
        if (len(relative.parts) != 3 or relative.parts[1:] != ('opencode', 'opencode.db')
                or not relative.parts[0].startswith(DATA_PREFIX)
                or not relative.parts[0][len(DATA_PREFIX):].isdigit()):
            return None
        allowed = {record.name + suffix for suffix in ('', '-wal', '-shm')}
        # Native per-launch data, never shared credentials. Each lies inside this launch's own data folder (the bundle
        # is that folder's opencode directory, moved whole); a link anywhere in it, or a folder it cannot read, refuses
        # the bundle, and the walk holds the record with everything in place.
        ancillary = {'log', 'repos', 'snapshot', 'tool-output'}
        children = list(record.parent.iterdir())
        if any(path.name not in allowed | ancillary for path in children):
            return None
        paths = list(children)

        def unreadable(error):
            raise error
        try:
            for root, folders, names in os.walk(record.parent, onerror=unreadable, followlinks=False):
                paths += [Path(root) / name for name in (*folders, *names)]
        except OSError:
            return None
        if any(path.is_symlink() or not (path.is_file() or path.is_dir()) for path in paths):
            return None
        files = list({path for path in paths if path.is_file()})
        return record.parent, kept.parent, files

    records = staticmethod(records)
    first_timestamp = staticmethod(first_timestamp)
    session_id = staticmethod(session_id)
    reader = staticmethod(read)

    def screen(self, text: str) -> Screen:
        """The box is the bar block straight above the status row, a pad row above and below its text."""
        if not text.strip():
            return Screen(text, box=None, starting=True)
        if (re.search(r'^\s*→ Asked [1-9][0-9]* questions?\s*$', text, re.M)
                and re.search(r'^\s*┃  [1-9][0-9]*\. Type your own answer\s*$', text, re.M)
                and re.search(r'^\s*┃  ↑↓ select  enter submit  esc dismiss\s*$', text, re.M)):
            return Screen(text, box=None, interruption=Interruption('native question tool', '', key=None))
        lines = text.rstrip("\n").split("\n")
        rule_at = next((i for i in range(len(lines) - 1, 0, -1) if RULE.match(lines[i]) and STATUS.match(lines[i - 1])), None)
        working = bool(WORKING.search(text))
        queued = sum(1 for line in lines if QUEUED_TAG.match(line))
        interruption = self.recognize(text)
        if rule_at is None or interruption is not None:
            unknown = None
            if rule_at is None and interruption is None:
                unknown = "the compact layout: widen the pane" if COMPACT.search(text) else "no box drawn as 1.19.5 draws it"
            return Screen(text, box=None, working=working, interruption=interruption, unknown=unknown, queued=queued)
        status_at = rule_at - 1
        i = status_at - 1
        while i >= 0 and _bar(lines[i]) is not None and not QUEUED_TAG.match(lines[i]):
            i -= 1
        top = i + 1
        if i >= 0 and QUEUED_TAG.match(lines[i]) and top < status_at and _bar(lines[top]) == "":
            top += 1  # the queued block's own closing pad
        rows = [_bar(line) for line in lines[top:status_at]]
        if len(rows) < 2 or rows[0] != "" or (rows[-1] != "" and not _wrapped_folder_pad(lines, rule_at)):
            return Screen(text, box=None, working=working, unknown="no box drawn as 1.19.5 draws it", queued=queued)
        body = [row or "" for row in rows[1:-1]]
        body = trimmed(body)
        box = "" if len(body) == 1 and PLACEHOLDER.match(body[0]) else "\n".join(body)
        return Screen(text, box=box, working=working, queued=queued)

    def human_wake_box_margin(self, pane_width: int) -> int:
        return 7  # declared composer insets, corroborated by the native paste

    def check_tool_evidence(self, record: Path) -> dict:
        from .check_evidence import opencode_tools
        return opencode_tools(record)

    def box_columns(self, pane_width: int) -> int | None:
        return pane_width - 7  # two cells of margin, the bar, two more cells, and a margin at the right

    def _box_geometry(self, pane_width: int, screen: Screen) -> tuple[int, int] | None:
        """1.19.5: the bottom rule spans the composer, including its five non-text cells."""
        if screen.box is None or screen.unknown or screen.interruption:
            return None
        lines = screen.text.rstrip('\n').split('\n')
        at = next((i for i in range(len(lines) - 1, 0, -1)
                   if RULE.match(lines[i]) and STATUS.match(lines[i - 1])), None)
        if at is None:
            return None
        rule = lines[at].rstrip(' ')
        if RULE.fullmatch(rule) is None:
            return None
        left = len(rule) - len(rule.lstrip(' '))
        columns = len(rule) - left - 5
        if columns < 1 or len(rule) > pane_width or rule[left:left + 1] != '╹':
            return None
        # Every row of the already-recognized composer must share this left edge.
        i = at - 1
        while i >= 0 and _bar(lines[i]) is not None and not QUEUED_TAG.match(lines[i]):
            if not lines[i].startswith(' ' * left + '┃'):
                return None
            i -= 1
        return columns, pane_width - left - 3

    def box_columns_for_screen(self, pane_width: int, screen: Screen) -> int | None:
        geometry = self._box_geometry(pane_width, screen)
        return geometry[0] if geometry else self.box_columns(pane_width)

    def box_comparison(self, screen: Screen, pane_width: int) -> str | None:
        geometry = self._box_geometry(pane_width, screen)
        if geometry is None:
            return screen.box
        columns, row_bound = geometry
        rows = []
        for row in screen.box.split('\n'):
            width, content = _cells(row), _cells(row.rstrip(' '))
            if width is None or content is None or width > row_bound or content > columns:
                return None
            # Remove only ASCII padding beyond the text area, for comparison alone.
            rows.append(row[:len(row) - max(0, width - columns)])
        return '\n'.join(rows)

    def box(self, text: str) -> Box:
        """Probed on 1.19.5: a paste of three lines or more, or a long one, folds to a count of its lines."""
        lines = text.rstrip("\n").count("\n") + 1
        return Box(text, folded=rf"\[Pasted ~{lines} lines\]", textwrap_ascii=True)

    def controls(self) -> tuple[Control, ...]:
        return (Control("model", launch="--model <pool>/<value>", session=None, confirmed_by="record"),)

    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("model picker", r"┃\s*/models\s+Switch model", key="Escape"),)
