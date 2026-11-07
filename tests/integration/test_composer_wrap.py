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
