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

import copy
import json
import re
from pathlib import Path

from .. import endpoint
from ..pricing import Price
from .endpoint import EndpointHarness
from ..ship import Pool, SeatSpec, ShipError
from .base import (Box, ship_template, trimmed, Control, Credential, Interruption, Keys, Launch, Screen, Tokens,
                   merged)

from .opencode_record import (DATA_PREFIX, RECORD, _Rows, _launch_number, data_dir_of, first_timestamp,
                              inputs, read, records, session_id, state)

CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "autoupdate": False,  # versions change when the operator says, and the status table says so
    "share": "disabled",  # a seat's conversation is the ship's, never a public link
}


# -- the adapter --------------------------------------------------------------------------------

BAR_TEXT = re.compile(r"^\s*┃  (.*)$")
BAR_BARE = re.compile(r"^\s*┃\s*$")
STATUS = re.compile(r"^\s*┃  (\S+) (\S+) · (.+?)\s*$")  # "Build auto · Qwen3.8 27B local-a": the agent, the permission mode, the model and pool as shown
RULE = re.compile(r"^\s*╹▀{3,}")
PLACEHOLDER = re.compile(r'^Ask anything(?:…|\.\.\.) "[^"]*"\s*$')  # a fresh box's hint, with a question that varies
WORKING = re.compile(r"\besc interrupt\b")
QUEUED_TAG = re.compile(r"^\s*┃\s+QUEUED\s*$")
COMPACT = re.compile(r"ctrl\+p cmd\s*$", re.M)


def _bar(line: str) -> str | None:
    """The text of one box row; the empty string for a bare bar; None for a line that is not a bar."""
    if BAR_BARE.match(line):
        return ""
    found = BAR_TEXT.match(line)
    return found.group(1) if found else None


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


class OpenCodeHarness(EndpointHarness):
    """OpenCode, proven on 1.19.5. Runs on an endpoint pool; the model changes only at launch."""

    name = "opencode"
    program = "opencode"
    home_variable = "XDG_DATA_HOME"
    keys = Keys(submit="Enter", send_now=None, interrupt="Escape Escape")  # Escape once opens nothing; twice, quickly, interrupts
    human_tools = ('question',)
    human_tool_policy = "The native question tool is denied for every configured agent."

    def build_home(self, home: Path, seat: SeatSpec, pool: Pool, ship_home: Path | None) -> Credential | None:
        home.mkdir(parents=True, exist_ok=True)
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
        layered = ship_template(ship_home, "opencode.json")
        config = copy.deepcopy(merged(merged(config, layered), seat.settings))
        if not seat.human_session:
            # V1 evaluates the last matching rule. Agent rules follow root rules.
            for scope in [config, *config.get('agent', {}).values(), *config.get('mode', {}).values()]:
                rules = scope.get('permission', {})
                rules = {'*': rules} if isinstance(rules, str) else dict(rules)
                rules.pop('question', None)
                scope['permission'] = {**rules, 'question': 'deny'}
                if isinstance(scope.get('tools'), dict) and 'question' in scope['tools']:
                    scope['tools']['question'] = False  # deprecated native setting must not translate back to allow
        price = Price.parse(pool.price) if pool.price is not None else None
        model = config["provider"][pool.name]["models"][choice.model]
        model.pop("cost", None)
        if price:
            model["cost"] = {"input": price.input, "output": price.output,
                             "cache_read": price.cache_read, "cache_write": price.cache_write}
        folder = home / "config" / "opencode"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "opencode.json").write_text(json.dumps(config, indent=2) + "\n")
        return self.endpoint_ready(home, choice, secret, price)

    def launch(self, seat: SeatSpec, pool: Pool, home: Path, brief: Path, resume: str | None, first: str | None) -> Launch:
        choice = endpoint.Choice.read(home)
        data = self._data_dir(home, resume, seat.name)
        if resume and not seat.human_session:
            if not all(question_stays_denied(raw) for raw in _Rows(data / RECORD, permissions=True).permissions.values()):
                raise ShipError(f'{seat.name}: saved native session permissions may re-enable question; '
                                'remove that override before restart, or use qd fresh for a new native session')
        argv = [self.program, "--auto", "--model", f"{pool.name}/{choice.model}"]  # --auto: no approval prompt (law: can-post)
        if resume:
            argv += ["--session", resume]
        if first:
            argv += ["--prompt", first]
        env = {"XDG_CONFIG_HOME": str(home / "config"), self.home_variable: str(data), "XDG_CACHE_HOME": str(home / "cache"),
               "XDG_STATE_HOME": str(home / "state"), "OPENCODE_DISABLE_AUTOUPDATE": "1", **endpoint.key(pool)}
        if not seat.human_session:
            # V1 loads project/config-env rules after its global config. Keep native
            # policy on the generated ship config rather than inheriting another scope.
            env.update(OPENCODE_DISABLE_PROJECT_CONFIG='1',
                       OPENCODE_CONFIG=str(home / 'config/opencode/opencode.json'),
                       OPENCODE_CONFIG_DIR=str(home / 'config/opencode'),
                       OPENCODE_CONFIG_CONTENT='{}', OPENCODE_PERMISSION='{"question":"deny"}')
        return Launch(tuple(argv), env, choice.price)

    @staticmethod
    def _data_dir(home: Path, resume: str | None, seat_name: str) -> Path:
        """A fresh launch gets the next numbered data directory; a resumed one, the directory holding its session."""
        existing = records(home)
        if resume:
            for record in reversed(existing):
                try:
                    if any(row[0] == resume for row in _Rows(record).sessions):
                        return data_dir_of(record)
                except OSError:
                    continue
            raise ShipError(f"{seat_name}: no record in {home} holds the session {resume}; qd fresh {seat_name} starts a new one")
        number = max((_launch_number(r) for r in existing), default=0) + 1
        return home / f"{DATA_PREFIX}{number:03d}"

    inputs = staticmethod(inputs)
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
        if len(rows) < 2 or rows[0] != "" or rows[-1] != "":
            return Screen(text, box=None, working=working, unknown="no box drawn as 1.19.5 draws it", queued=queued)
        body = [row or "" for row in rows[1:-1]]
        body = trimmed(body)
        box = "" if len(body) == 1 and PLACEHOLDER.match(body[0]) else "\n".join(body)
        return Screen(text, box=box, working=working, queued=queued)

    def box_columns(self, pane_width: int) -> int | None:
        return pane_width - 7  # two cells of margin, the bar, two more cells, and a margin at the right

    def box(self, text: str) -> Box:
        """Probed on 1.19.5: a paste of three lines or more, or a long one, folds to a count of its lines."""
        lines = text.rstrip("\n").count("\n") + 1
        return Box(text, folded=rf"\[Pasted ~{lines} lines\]")

    def controls(self) -> tuple[Control, ...]:
        return (Control("model", launch="--model <pool>/<value>", session=None, confirmed_by="record"),)

    def interruptions(self) -> tuple[Interruption, ...]:
        return (Interruption("model picker", r"┃\s*/models\s+Switch model", key="Escape"),)
