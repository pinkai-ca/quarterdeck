"""A request observer cannot consume a producer's unfinished acknowledgment."""

import os
import threading
from pathlib import Path

import pytest

from quarterdeck import service, ship as shipmod
from tests.integration import progress


@pytest.mark.parametrize("paused_at", ["created", "written"])
@pytest.mark.parametrize("outcome", ["release", "timeout", "exception"])
def test_progress_waits_for_publication_and_relinquished_ownership(drydock, monkeypatch, paused_at, outcome):
    ship = shipmod.load(drydock)
    svc = service.Service(ship, None)
    svc.ledger.close()  # requests() needs no database or panes
    folder = ship.runtime / "requests"
    paused, release, observed = threading.Event(), threading.Event(), threading.Event()
    published = []
    paused_results = []
    publication = threading.Condition()
    threads = []
    original_write, original_replace = os.write, os.replace
    original_path_replace = Path.replace
    original_wait = progress.wait_for

    def publish(path, target):
        result = original_path_replace(path, target)
        if path.parent == folder and path.suffix == ".tmp":
            with publication:
                published.append(Path(target))
                publication.notify_all()
        return result

    def write(descriptor, data):
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        if (paused_at == "created" and path.parent == folder and ".done." in path.name
                and path.suffix == ".tmp" and not paused.is_set()):
            paused.set()
            assert release.wait(5), "test failed to release the acknowledgment writer"
        return original_write(descriptor, data)

    def rename(source, target, *args, **kwargs):
        result = original_replace(source, target, *args, **kwargs)
        path = Path(target)
        if paused_at == "written" and path.parent == folder and path.suffix == ".done" and not paused.is_set():
            paused.set()  # complete answer published; active request/claim still owned
            assert release.wait(5), "test failed to release the acknowledgment writer"
        return result

    def wait(predicate, **kwargs):
        if outcome == "timeout":
            assert paused.wait(2), "timeout must exercise a request the producer owns"
            kwargs["seconds"] = 0.2
        def poll():
            result = predicate()
            if paused.is_set() and not release.is_set():
                paused_results.append(result)
                observed.set()
                if outcome == "exception":
                    raise RuntimeError("synthetic observer failure")
            return result
        return original_wait(poll, **kwargs)

    def start(fn):
        result = {}
        def run():
            try:
                fn()
                result["finished"] = True
            except Exception as error:
                result["error"] = error
        thread = threading.Thread(target=run)
        threads.append(thread)
        thread.start()
        return thread, result

    monkeypatch.setattr(Path, "replace", publish)
    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "replace", rename)
    monkeypatch.setattr(progress, "wait_for", wait)
    observer, observer_result = start(lambda: progress.service_progress(ship, seconds=3))
    try:
        with publication:
            assert publication.wait_for(lambda: len(published) == 1, timeout=2)
        producer, producer_result = start(svc.requests)
        assert paused.wait(2) and observed.wait(2)
        assert paused_results and not any(paused_results), "an owned request cannot acknowledge progress"
        request = published[0]
        done = request.with_suffix(".done")
        if outcome == "release":
            assert observer.is_alive() and observer_result == {}, "an unfinished acknowledgment cannot finish or fail the observer"
        else:
            observer.join(2)
            assert not observer.is_alive(), "failure cleanup must remain bounded"
            error = observer_result.get("error")
            if outcome == "timeout":
                assert isinstance(error, AssertionError) and "condition not met" in str(error)
            else:
                assert isinstance(error, RuntimeError) and str(error) == "synthetic observer failure"
        assert request.exists(), "only the service may remove its published request, even on observer failure"
        assert request.with_suffix(".claimed").read_text() == request.read_text(), "the service retains its replay guard"
        temporary = list(folder.glob(f".{done.name}.*.tmp"))
        if paused_at == "created":
            assert not done.exists(), "an unpublished temporary is never a final acknowledgment"
            assert len(temporary) == 1 and temporary[0].read_text() == "", "observer leaves the producer's temporary alone"
        else:
            assert not temporary
            assert done.read_text() == "unknown request 'test-progress'\n", "observer leaves the complete owned answer alone"
        release.set()
        producer.join(2)
        assert not producer.is_alive() and producer_result == {"finished": True}, "no service duty error"
        if outcome == "release":
            with publication:
                assert publication.wait_for(lambda: len(published) == 2, timeout=2)
            assert published[0] != published[1]
            svc.requests()
            observer.join(2)
            assert not observer.is_alive() and observer_result == {"finished": True}
            assert not list(folder.iterdir()), "completed acknowledgments are cleaned"
        else:
            assert len(published) == 1 and not request.exists()
            assert done.read_text() == "unknown request 'test-progress'\n"
            done.unlink()  # test teardown, after the producer has finished
            assert not list(folder.iterdir())
    finally:
        release.set()
        for thread in threads:
            thread.join(4)
        assert not any(thread.is_alive() for thread in threads)
