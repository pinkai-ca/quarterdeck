"""Short report envelopes through a fake Claude process with literal word wrapping."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quarterdeck import harness, pending, post, seat as seatmod, ship as shipmod
from quarterdeck.door import Door

from .waiting import wait_for


# Only the fake's renderer differs: emulate the observed literal, padded Claude
# rows instead of folding every multiline input. It keeps the fake's real PTY,
# input handling and Claude-shaped record writer; no native program is launched.
PROGRAM = '''
import os
import sys
import textwrap
from quarterdeck.harness import simulator

class LiteralClaude(simulator.Program):
    def shown_box(self):
        if self.box.count("\\n") >= 3:
            return super().shown_box()
        columns = os.get_terminal_size().columns - 4
        rows = []
        for line in self.box.rstrip("\\n").split("\\n"):
            rows.extend(textwrap.wrap(line, width=columns, break_on_hyphens=False,
                                      replace_whitespace=False) or [""])
        return "\\n".join(row.ljust(columns + 2) for row in rows)

    def draw_box(self):
        lines = [simulator.RULE,
                 *[("❯ " if i == 0 else "  ") + row
                   for i, row in enumerate(self.shown_box().split("\\n"))],
                 simulator.RULE,
                 f"fake {self.args.shape} · model {self.model} · effort {self.effort} · {self.status()}"]
        sys.stdout.write("\\r\\n".join(lines) + "\\r")
        sys.stdout.flush()
        self.box_lines = len(lines) - 1

simulator.Program = LiteralClaude
raise SystemExit(simulator.main())
'''


@pytest.fixture(params=[False, True], ids=['idle', 'turn-starting'])
def literal_claude(drydock, tmux, tmp_path, monkeypatch, request):
    declaration = drydock / 'ship.toml'
    declaration.write_text(declaration.read_text() +
                           '\n[seats.captain.settings]\nsteps = "30"\nstep_seconds = "1"\n')
    current = shipmod.load(drydock)
    seat = seatmod.Seat(current, current.seat('captain'), tmux)
    fake, claude = seat.harness, harness.get('claude')
    program = tmp_path / 'literal_claude.py'
    program.write_text(PROGRAM)
    launch = fake.launch

    def synthetic_launch(*args, **kwargs):
        found = launch(*args, **kwargs)
        return replace(found, argv=(found.argv[0], str(program), *found.argv[3:]))

    monkeypatch.setattr(fake, 'launch', synthetic_launch)
    seat.up()
    wait_for(lambda: Door(seat).reading().open)
    fake_screen = fake.screen

    def screen(text):
        # The fake's status supplies its synthetic activity; the real Claude
        # parser and matcher read the literal composer layout under test.
        return replace(claude.screen(text), working=fake_screen(text).working)

    monkeypatch.setattr(fake, 'screen', screen)
    monkeypatch.setattr(fake, 'box', claude.box)
    monkeypatch.setattr(fake, 'box_columns', claude.box_columns)
    wait_for(lambda: Door(seat).reading().open)
    if request.param:
        assert Door(seat).deliver('Begin an ordinary synthetic turn.').result == 'delivered'
        wait_for(lambda: Door(seat).screen().working)
    calls = dict(paste=0, submit=0)
    paste, keys = tmux.paste, tmux.keys

    def track_paste(*args, **kwargs):
        calls['paste'] += 1
        return paste(*args, **kwargs)

    def track_keys(*args, **kwargs):
        if args[1:] == ('Enter',):
            calls['submit'] += 1
        return keys(*args, **kwargs)

    monkeypatch.setattr(tmux, 'paste', track_paste)
    monkeypatch.setattr(tmux, 'keys', track_keys)
    return seat, calls, request.param


def report(seat, kind):
    # A whole 995-character typed form: no inbox footer, one body paragraph.
    message = post.Message(f'{kind[:3]}-20000101-0000-example-a1b2', kind, 'example', seat.name, post.now(), '')
    size = 995 - len(post.typed([message]))
    prefix = 'A' * 70 + ' wrap-target-word '
    body = (prefix + 'ordinary words ' * 100)[:size - 1].rstrip() + '.'
    body = body.ljust(size, '.')
    message = replace(message, body=body)
    text = post.typed([message])
    assert len(text) == 995 and text.count('\n') == 2
    assert 'inbox:' not in text and text.endswith(body + '\n')
    return text


@pytest.mark.parametrize('kind', ['report', 'answer'])
def test_unfolded_report_is_pasted_submitted_and_record_confirmed_once(literal_claude, kind):
    seat, calls, working = literal_claude
    text = report(seat, kind)
    result = Door(seat).deliver(text)
    assert result.result == ('queued' if working else 'delivered'), result
    assert calls == dict(paste=1, submit=1)
    received = [item for item in seat.harness.inputs(seat.record()) if item.text == text.rstrip('\n')]
    assert len(received) == 1 and received[0].kind == ('queued' if working else 'accepted')


@pytest.mark.parametrize('kind', ['report', 'answer'])
def test_submit_finds_the_saved_report_in_its_wrapped_box(literal_claude, kind):
    seat, calls, working = literal_claude
    text = report(seat, kind)
    door = Door(seat)
    door.tmux.paste(seat.pane().id, text)
    screen = wait_for(lambda: (s if s and s.box and 'wrap-target-word' in s.box else None)
                      if (s := door.screen()) else None)
    assert '[Pasted text' not in screen.box
    proof = SimpleNamespace(data=dict(phase='pasting', payload=text), same_launch=lambda _: True)
    candidate, refusal = pending._submit_candidate(door, screen, seat.pane(), [proof])
    assert candidate is proof and refusal == ''
    door.submit(seat.pane().id)
    assert door.confirm(text).result == ('queued' if working else 'delivered')
    assert calls == dict(paste=1, submit=1)
