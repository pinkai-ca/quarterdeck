"""A creator's own private server that is already gone, or going, when its close arrives counts as closed.

The server here ends on its own kill-server (as a resumed server runs the one its creator queued while it was
stopped) just as the creator's close sends its own; the process takes a moment to leave the process table, as it does
on a loaded machine. The close finds no server on its own socket, waits for that server's own exit, and removes the
namespace. Every server here is this test's own, started and ended by its own -S socket.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

from quarterdeck import tmux as tmuxmod, tmux_socket
from quarterdeck.tmux import Tmux


def test_a_server_already_ending_when_its_creator_closes_it_counts_as_closed(monkeypatch):
    server = Tmux.private(Path(os.environ['TMUX_TMPDIR']))
    try:
        server.new_session('Synthetic')
        pid, socket, folder = server._owned.pid, server.socket, server._owned.namespace.folder
        real_run, real_ended = Tmux._run, tmux_socket.OwnedSocket.ended
        gone = {}

        def run(self, *args, **kwargs):
            if args[:1] == ('if-shell',) and not gone:
                # The server's own kill-server, already on its way, reaches it first.
                subprocess.run(['tmux', '-S', socket, '-N', '-f', '/dev/null', 'kill-server'],
                               capture_output=True, timeout=10, check=False)
                gone['at'] = time.monotonic()
            return real_run(self, *args, **kwargs)

        def ended(self):
            # Mid-exit, as under load: the process is still in the table for half a second after its socket is shut.
            return real_ended(self) and ('at' in gone and time.monotonic() - gone['at'] >= .5)

        monkeypatch.setattr(Tmux, '_run', run)
        monkeypatch.setattr(tmux_socket.OwnedSocket, 'ended', ended)
        assert server.close(), server.held
        assert server.found_gone, 'the close says its server was already gone'
        assert gone, 'the close sent its own kill after the server had begun to end'
        assert not Path(socket).exists() and not folder.exists(), 'the namespace is gone with its server'
        fields = tmux_socket.process_state(pid)
        assert not fields or fields[0] in tmux_socket.EXITED, 'its server has exited'
    finally:
        monkeypatch.undo()
        if not server._closed and not server.close():
            raise AssertionError(f'private tmux cleanup held: {server.held}')
