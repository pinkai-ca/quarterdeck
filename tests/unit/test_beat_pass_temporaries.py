"""The service's beat (its own thread) and its main loop's pass stamp never write through the same temporary file.

Both are written whole by rename into runtime/heartbeat. A shared temporary let one writer's rename carry the other's
content over its file: the pass JSON over the beat read 'busy (heartbeat unknown)', and down inside a jail. Here the
two writers race in threads, and every reading of either file parses as its own kind.
"""
import json
import threading

from quarterdeck import post_dir, servicectl

WRITES = 3000


def test_the_beat_and_the_pass_stamp_racing_each_stay_their_own_kind(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    servicectl.write_heartbeat(runtime)
    servicectl.write_pass(runtime, 'pass')
    done, wrong = threading.Event(), []

    def beats():
        for _ in range(WRITES):
            servicectl.write_heartbeat(runtime)

    def stamps():
        for index in range(WRITES):
            servicectl.write_pass(runtime, f'duty{index % 7}')

    def reader():
        beat_path, pass_path = servicectl.heartbeat_path(runtime), servicectl.pass_path(runtime)
        while not done.is_set():
            beat = beat_path.read_text()
            stamp = pass_path.read_text()
            if beat.startswith('{') or len(beat.split()) != 2:
                wrong.append(('beat', beat[:60]))
            try:
                json.loads(stamp)['duty']
            except (ValueError, KeyError, TypeError):
                wrong.append(('pass', stamp[:60]))

    writers = [threading.Thread(target=beats), threading.Thread(target=stamps)]
    watcher = threading.Thread(target=reader)
    watcher.start()
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    done.set()
    watcher.join()
    assert wrong == [], wrong[:5]
    assert servicectl.read_heartbeat(runtime) is not None and servicectl.read_pass(runtime) is not None


def test_each_writes_through_a_temporary_of_its_own_and_a_leftover_one_is_cleared(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    servicectl.write_heartbeat(runtime)
    servicectl.write_pass(runtime, 'pass')
    folder = servicectl.heartbeat_path(runtime).parent
    assert sorted(p.name for p in folder.iterdir()) == ['service.heartbeat', 'service.pass'], 'no temporary left'
    # A writer cut short leaves its own hidden temporary, named after its target; the next start or stop clears it.
    for name in ('service.heartbeat', 'service.pass'):
        (folder / post_dir.temporary_name(name)).write_text('cut short')
    (folder / 'service.tmp').write_text('left by the earlier code')
    servicectl.remove_heartbeat(runtime)
    assert list(folder.iterdir()) == []
