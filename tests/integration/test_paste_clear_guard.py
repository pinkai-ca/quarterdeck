"""Real private transport, synthetic terminal; no native model client/provider."""
import sys
import shlex

import pytest

from quarterdeck.tmux import TmuxError
from .waiting import wait_for


def test_clear_key_queue_refuses_changed_pid_or_cursor_and_preserves_the_next_owner(tmp_path,tmux):
    output=tmp_path/'keys';ready=tmp_path/'ready'
    script=tmp_path/'terminal.py'
    script.write_text(f'''import os,tty
from pathlib import Path
tty.setraw(0)
os.write(1,b'editor ready')
Path({str(ready)!r}).write_text('ready')
data=b''
while len(data)<3:
 data+=os.read(0,3-len(data))
Path({str(output)!r}).write_bytes(data)
''')
    tmux.new_session('Synthetic editor')
    pane=tmux.new_window('Synthetic editor','terminal',tmp_path,['cat'])
    previous_pid=pane.pid
    tmux._invoke('respawn-pane','-k','-t',pane.id,shlex.join([sys.executable,str(script)]))
    wait_for(ready.exists)
    pane=tmux.pane(pane.id)
    assert pane is not None and pane.pid!=previous_pid
    def positioned():
        found=tmux.editor_position(pane.id,pane.pid)
        return found if found and found[0]==len('editor ready') else None
    position=wait_for(positioned)
    assert position is not None
    keys=('C-a','C-k','BSpace')
    with pytest.raises(TmuxError,match='changed'):
        tmux.clear_owned_paste(pane.id,previous_pid,position,keys)
    with pytest.raises(TmuxError,match='changed'):
        tmux.clear_owned_paste(pane.id,pane.pid,(position[0]+1,*position[1:]),keys)
    tmux.clear_owned_paste(pane.id,pane.pid,position,keys)
    assert wait_for(lambda:output.read_bytes() if output.exists() else None)==b'\x01\x0b\x7f'  # law: identity
