"""Terminal wrap flags, not punctuation, determine zero-character row breaks."""

import shlex
import time

import pytest

from quarterdeck.harness import get


@pytest.mark.parametrize('name,prefix,footer', [
    ('codex', '› ', '\n\n  test low · /work'),
    ('claude', '❯ ', '\n' + '─'*20),
    ('antigravity', '> ', '\n' + '─'*20),
    ('fake', '> ', '\n' + '─'*20 + '\nfake claude · model test · effort low · idle'),
])
@pytest.mark.parametrize('middle', ['fresh-', 'abc/xy', 'normal'])
def test_terminal_soft_wrap_and_real_newline_are_distinct(tmux, tmp_path, name, prefix, footer, middle):
    # The private fixture is mandatory: never the default/live tmux server.
    tmux.new_session('Wrap')
    pane = tmux.new_window('Wrap', 'display', tmp_path, ['cat'])
    tmux.run('resize-window', '-t', pane.id, '-x', '40', '-y', '24')
    payload = 'x'*(40-len(prefix)-len(middle)) + middle + 'tail'
    def show(text):
        script = tmp_path/'display.sh'
        output = '\x1b[2J\x1b[H' + '─'*20+'\r\n'+prefix+text+footer.replace('\n','\r\n')+'\r\n'
        script.write_text('printf %s '+shlex.quote(output)+'\nsleep 30\n')
        tmux.run('respawn-pane','-k','-t',pane.id,'sh '+shlex.quote(str(script)))
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            screen = get(name).screen(tmux.capture(pane.id))
            if screen.box is not None and 'tail' in screen.box:
                return screen
            time.sleep(.02)
        raise AssertionError('synthetic screen not drawn')
    screen = show(payload)
    assert screen.box == payload
    assert get(name).box(payload).matches(screen.box)  # law: one-door
    # Same characters, deliberate short hard break: -J must not erase it.
    hard = show(payload[:5]+'\r\n  '+payload[5:])
    assert '\n' in hard.box and not get(name).box(payload).matches(hard.box)


@pytest.mark.parametrize('name', ['codex','claude','antigravity','fake'])
@pytest.mark.parametrize('body', ['', 'payload  with two spaces\nnext line'])
def test_real_private_capture_retains_padding_and_adapters_read_it(tmux, tmp_path, monkeypatch, name, body):
    from quarterdeck.door import Door
    from types import SimpleNamespace
    from tests.contract.test_padded_composer import capture

    monkeypatch.setattr(Door, 'wall', lambda self, screen: None)
    tmux.new_session('Padding')
    script = tmp_path/'padded.sh'
    output = '\x1b[2J\x1b[H'+capture(name,body,'full',80).replace('\n','\r\n')
    script.write_text('printf %s '+shlex.quote(output)+'\nsleep 30\n')
    pane = tmux.new_window('Padding','display',tmp_path,['sh',str(script)])
    tmux.run('resize-window','-t',pane.id,'-x','80','-y','24')
    seat = SimpleNamespace(harness=get(name),session=SimpleNamespace(human=False),spec=SimpleNamespace(),
                           pane=lambda:tmux.pane(pane.id),tmux=tmux)
    door = Door(seat)
    deadline = time.monotonic()+3
    while time.monotonic()<deadline:
        screen = door.screen()
        if screen and screen.box is not None:
            break
        time.sleep(.02)
    assert any(line.endswith('   ') for line in tmux.capture(pane.id).splitlines()), 'exercise actual -J padding'
    assert door.reading().open is (not body)
    if body:
        assert door.matches(get(name).box(body),screen)
        assert not door.matches(get(name).box(body.replace('  with',' with')),screen)


@pytest.mark.parametrize("name", ["codex", "pi"])
def test_flag_wrap_in_a_private_318_column_pane(tmux, tmp_path, monkeypatch, name):
    from types import SimpleNamespace
    from quarterdeck.door import Door
    from tests.contract.test_flag_wrap import example
    from tests.contract.test_padded_composer import capture

    monkeypatch.setattr(Door, "wall", lambda self, screen: None)
    payload, rows = example(name)
    tmux.new_session("Flags")
    pane = tmux.new_window("Flags", "display", tmp_path, ["cat"])
    tmux.run("resize-window", "-t", pane.id, "-x", "318", "-y", "71")
    output = "\x1b[2J\x1b[H" + capture(name, rows, "full", 318).replace("\n", "\r\n")
    script = tmp_path / "display.sh"
    script.write_text("printf %s " + shlex.quote(output) + "\nsleep 30\n")
    tmux.run("respawn-pane", "-k", "-t", pane.id, "sh " + shlex.quote(str(script)))
    seat = SimpleNamespace(harness=get(name), tmux=tmux, pane=lambda: tmux.pane(pane.id))
    door = Door(seat)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        screen = door.screen()
        if screen and screen.box and "before the flag" in screen.box:
            break
        time.sleep(0.02)
    assert seat.pane().width == 318 and seat.pane().height == 71
    assert any(row.endswith("   ") for row in tmux.capture(pane.id).splitlines())
    assert door.matches(get(name).box(payload), screen)
    assert not door.matches(get(name).box(payload.replace("alpha alpha", "alpha  alpha", 1)), screen)
