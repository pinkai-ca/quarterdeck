"""Allocate room and compose the ANSI output shared by the viewer and its cast."""

from collections import deque
from dataclasses import dataclass, field
import json
import re
import time
from unicodedata import category, east_asian_width

ARRIVAL = 4.0
LINGER = 3.0
MOVE = 0.34
BETWEEN_MOVES = 2.0
MIN_STRIP = 7
ARRIVAL_STRIP = 10

RESET = '\033[0m'
PINK = '\033[95m'
ESCAPES = re.compile(
    r'\x1b\[[0-?]*[ -/]*[@-~]|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)'
    r'|\x1b[P^_X][^\x1b\x9c]*(?:\x1b\\|\x9c|$)|\x1b[ -/]*[0-~]'
)
SGR = re.compile(r'\x1b\[[0-9:;]{0,128}m')


@dataclass
class Activity:
    active: bool = False
    known: bool = False
    began: float = float('-inf')
    arrived: float = float('-inf')
    ended: float = float('-inf')

    def observe(self, active: bool | None, now: float):
        self.known = active is not None
        if active is True and not self.active:
            self.active, self.began = True, now
        elif active is False and self.active:
            self.active, self.ended = False, now
            if self.arrived <= self.began:
                self.arrived = float('-inf')

    def arriving(self, now):
        return now - self.arrived < ARRIVAL and self.arrived >= self.began

    def rank(self, now, opened):
        if self.arriving(now):
            return 3, False, self.arrived
        if self.active:
            return 2, opened, self.began
        if now - self.ended < LINGER:
            return 1, opened, self.ended
        return 0, False, 0.0


@dataclass
class Room:
    columns: tuple[tuple[str, ...], ...]
    activity: dict[str, Activity]
    rows: int = 0
    previous: list[dict] = field(default_factory=list)
    target: list[dict] = field(default_factory=list)
    changed: float = float('-inf')

    def _plan(self, now, rows):
        result = []
        for index, column in enumerate(self.columns):
            free = rows - len(column)
            if free <= 0:
                result.append({None: 0})
                continue
            current = self.target[index] if self.target else {}
            rank = {name: self.activity[name].rank(now, current.get(name, 0) > 0) for name in column}
            wanted = sorted((name for name in column if rank[name][0]), key=rank.__getitem__, reverse=True)
            opened = wanted[:max(1, free // MIN_STRIP)]
            if not opened:
                result.append({None: free})
                continue
            thin = [name for name in opened if rank[name][0] == 3]
            rest = [name for name in opened if rank[name][0] != 3]
            if not rest:
                thin, rest = [], opened
            strip = min(ARRIVAL_STRIP, free // len(opened))
            base, extra = divmod(free - len(thin) * strip, len(rest))
            sizes = dict.fromkeys(thin, strip)
            sizes.update({name: base + (i < extra) for i, name in enumerate(rest)})
            result.append(sizes)
        return result

    def update(self, now, rows):
        if rows != self.rows or not self.target:
            self.rows = rows
            self.previous = self.target = self._plan(now, rows)
            self.changed = float('-inf')  # a terminal resize is shown immediately at its new dimensions
        elif now - self.changed >= BETWEEN_MOVES:
            target = self._plan(now, rows)
            if target != self.target:
                self.previous, self.target = self.target, target
                self.changed = now
        progress = min(1.0, (now - self.changed) / MOVE)
        if progress >= 1.0:
            return self.target
        # Cubic ease in/out, with largest-remainder rounding so no row disappears mid-transition.
        weight = 4 * progress**3 if progress < 0.5 else 1 - 4 * (1 - progress)**3
        return [_interpolate(before, after, weight) for before, after in zip(self.previous, self.target)]


def _interpolate(before, after, weight):
    keys = dict.fromkeys((*before, *after))
    exact = {key: before.get(key, 0) * (1 - weight) + after.get(key, 0) * weight for key in keys}
    rows = {key: int(value) for key, value in exact.items()}
    extra = sum(after.values()) - sum(rows.values())
    for key in sorted(keys, key=lambda key: exact[key] - rows[key], reverse=True)[:extra]:
        rows[key] += 1
    return rows


def widths(total, count):
    base, extra = divmod(total - (count - 1), count)
    return [base + (i < extra) for i in range(count)]


def _cells(char):
    if category(char) in ('Mn', 'Me'):
        return 0
    return 2 if east_asian_width(char) in ('W', 'F') else 1


def _clip(text, width):
    output = []
    used = 0
    for char in text:
        if category(char).startswith('C') or char in ('\u2028', '\u2029'):
            continue
        size = _cells(char)
        if used + size > width:
            return ''.join(output), used, True
        if size or used:
            output.append(char)
        used += size
    return ''.join(output), used, False


def label(title, width, badge=''):
    rule = '── '
    if badge and width >= len(badge) + 8:
        name, used, _ = _clip(title, width - len(rule) - len(badge) - 1)
        name += ' ' + badge
        used += len(badge) + 1
    else:
        name, used, _ = _clip(title, max(0, width - len(rule) - 1))
    return f'{PINK}{rule}{RESET}{name} {PINK}{"─" * max(0, width - len(rule) - used - 1)}{RESET}'


def screen_rows(text, width, height):
    """Crop styled, physical screen rows. Only text and SGR reach the viewer."""
    rows = []
    style = deque(maxlen=32)
    for line in text.removesuffix('\n').split('\n'):
        displayed, cells, offset, clipped = [''.join(style)], 0, 0, False
        for match in ESCAPES.finditer(line):
            if not clipped:
                shown, used, clipped = _clip(line[offset:match.start()], width - cells)
                displayed.append(shown)
                cells += used
            if SGR.fullmatch(match[0]):
                control = match[0]
                if control in ('\033[m', RESET):
                    style.clear()
                else:
                    style.append(control)
                if not clipped:
                    displayed.append(control)
            offset = match.end()
        if not clipped:
            shown, used, _ = _clip(line[offset:], width - cells)
            displayed.append(shown)
            cells += used
        rows.append(''.join(displayed) + RESET + ' ' * (width - cells))
    visible = rows[-height:] if height else []
    return [' ' * width] * (height - len(visible)) + visible


def compose(columns, plans, sizes, titles, screens, activity, now):
    output = []
    for column, plan, width in zip(columns, plans, sizes):
        rows = [' ' * width] * plan.get(None, 0)
        for name in column:
            height = plan.get(name, 0)
            if height:
                rows.extend(screen_rows(screens.get(name, ''), width, height))
            state = activity[name]
            badge = ('working' if state.active and state.known else 'incoming' if state.arriving(now)
                     else 'unread' if state.active else '')
            rows.append(label(titles[name], width, badge))
        output.append(rows)
    return tuple(f'{PINK}│{RESET}'.join(row) for row in zip(*output))


class TerminalFrames:
    """Emit complete rows at their terminal coordinates. A cast receives exactly these updates."""

    def __init__(self, terminal, cast=None, *, clock=time.monotonic):
        self.terminal, self.cast, self.clock = terminal, cast, clock
        self.started = clock()
        self.size = self.rows = None

    def _cast(self, value):
        self.cast.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
        self.cast.flush()

    def close(self):
        if self.cast is not None and self.size is not None:
            self._cast([self.clock() - self.started, 'o', ''])  # hold the last frame through exit

    def paint(self, size, rows):
        if size != self.size:
            if self.cast is not None:
                width, height = size
                if self.size is None:
                    self._cast(dict(version=2, width=width, height=height, env={'TERM': 'xterm-256color'}))
                else:
                    self._cast([self.clock() - self.started, 'r', f'{width}x{height}'])
            self.size, self.rows = size, None
        output = []
        if self.rows is None:
            output.append('\033[?25l\033[?7l\033[2J')
        for index, row in enumerate(rows):
            if self.rows is None or index >= len(self.rows) or row != self.rows[index]:
                output.append(f'\033[{index + 1};1H\033[2K{row}{RESET}')
        for index in range(len(rows), len(self.rows or ())):
            output.append(f'\033[{index + 1};1H\033[K')
        self.rows = rows
        if output:
            text = ''.join(output)
            self.terminal.write(text)
            self.terminal.flush()
            if self.cast is not None:
                self._cast([self.clock() - self.started, 'o', text])
