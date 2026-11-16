"""Force the released owner's exit before stop's lifetime reading, using pipe events only."""

import os

from quarterdeck import servicectl, shutdown
from tests.unit import test_stop_owner_lifetime as lifetime


def test_early_owner_exit_keeps_the_discovered_child_stop_proof(tmp_path, monkeypatch, capsys):
    native_kill = os.kill
    original_clock, original_stop = lifetime.stopping_clock, servicectl.stop
    seen = {}

    def stopping_clock(patch, child, response='ignore'):
        signals = original_clock(patch, child, response=response)
        send = os.kill
        seen['child'] = child

        def exit_before_read(pid, sig):
            if pid in child.descendants:
                # The original test blocks descendant signals inside stop, then
                # restores this sender for its own pidfd-confirmed teardown.
                return native_kill(pid, sig)
            result = send(pid, sig)
            if sig == lifetime.signal.SIGTERM and response == 'release-tree':
                child.stdin.write('x')
                child.stdin.flush()
                child.wait(timeout=20)
                seen['owner_ended_before_read'] = True
            return result

        patch.setattr(servicectl.os, 'kill', exit_before_read)
        return signals

    def observed_stop(runtime):
        try:
            result = original_stop(runtime)
        except Exception as error:
            seen['exception'] = type(error).__name__ + ': ' + str(error)
            raise
        else:
            seen['returned'] = result
            child = seen['child']
            seen['live_descendants_at_return'] = [pid for pid, start in child.descendants.items()
                if not shutdown.process_gone(dict(pid=pid, start=start))]
            return result

    monkeypatch.setattr(lifetime, 'stopping_clock', stopping_clock)
    monkeypatch.setattr(servicectl, 'stop', observed_stop)
    try:
        lifetime.test_a_child_discovered_during_the_kill_holds_both_restart_paths(tmp_path, monkeypatch)
    finally:
        with capsys.disabled():
            print('EARLY_OWNER_EXIT', {key: value for key, value in seen.items() if key != 'child'}, flush=True)
