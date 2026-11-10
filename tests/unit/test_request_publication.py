"""The real ordinary writer publishes a complete request before the service can see it."""

from dataclasses import replace
import json
import os
from pathlib import Path
import threading

import pytest

from quarterdeck import door as doormod, durable, servicectl
from .test_request_isolation import svc


@pytest.mark.parametrize("phase", ["created", "written"])
def test_an_ordinary_request_is_invisible_until_publication_then_runs_once_with_its_reply(svc, monkeypatch, phase):
    service, folder = svc
    service.ship.seats["healthy"] = replace(service.ship.seat("survivor"), name="healthy")
    called = []
    monkeypatch.setattr(doormod.Door, "interrupt", lambda self: called.append(self.seat.name) or f"{self.seat.name} stopped")
    monkeypatch.setattr(servicectl, "alive", lambda _: True)  # the writer talks only to this synthetic consumer
    monkeypatch.setattr(servicectl, "ANSWER_SECONDS", 3)
    paused, release, published = threading.Event(), threading.Event(), threading.Event()
    result = {}
    original_text, original_write, original_replace = Path.write_text, os.write, os.replace

    def pause():
        paused.set()
        assert release.wait(5), "producer was not released"

    def text_write(path, text, *args, **kwargs):
        if threading.current_thread() is not producer or path.parent != folder or path.suffix != ".json":
            return original_text(path, text, *args, **kwargs)
        # Exercise the old real writer too: its open descriptor survives a consumer rename.
        with path.open("w") as stream:
            if phase == "created":
                pause()
            count = stream.write(text)
            stream.flush()
            if phase == "written":
                pause()
        published.set()
        return count

    def fd_write(descriptor, data):
        try:
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            return original_write(descriptor, data)
        if threading.current_thread() is not producer or path.parent != folder or path.suffix != ".tmp" or paused.is_set():
            return original_write(descriptor, data)
        if phase == "created":
            pause()
        count = original_write(descriptor, data)
        if phase == "written":
            pause()
        return count

    def publish(source, target, *args, **kwargs):
        value = original_replace(source, target, *args, **kwargs)
        path = Path(target)
        if threading.current_thread() is producer and path.parent == folder and path.suffix == ".json":
            published.set()
        return value

    def request():
        try:
            result["answer"] = servicectl.request(service.runtime, "stop", seat="survivor")
        except BaseException as error:
            result["error"] = error

    producer = threading.Thread(target=request)
    monkeypatch.setattr(Path, "write_text", text_write)
    monkeypatch.setattr(os, "write", fd_write)
    monkeypatch.setattr(os, "replace", publish)
    producer.start()
    try:
        assert paused.wait(2), "the real writer must be held before final publication"
        visible_while_writing = list(folder.glob("*.json"))
        later = folder / "zz-healthy.json"
        durable.replace_file(later, json.dumps(dict(kind="stop", seat="healthy")))
        service.requests()
        assert called == ["healthy"], "the unpublished action must not run; the later healthy request must progress"
        assert later.with_suffix(".done").read_text() == "healthy stopped\n"
        assert not (folder / "failed").exists(), "the writer's unfinished file must never be treated as permanently malformed"
        assert not visible_while_writing, "only complete final .json files may be visible to the consumer"
        later.with_suffix(".done").unlink()
        release.set()
        assert published.wait(2)
        service.requests()
        producer.join(4)
        assert not producer.is_alive() and result == {"answer": "survivor stopped"}
        assert called == ["healthy", "survivor"]
        service.requests()
        assert called == ["healthy", "survivor"], "publication, reply and retirement execute the valid action once"
        assert not list(folder.glob("*.json")) and not list(folder.glob("*.claimed"))
        assert not list(folder.glob("*.done")) and not list(folder.glob("*.tmp"))
    finally:
        release.set()
        producer.join(4)
        assert not producer.is_alive(), "no synthetic request writer may survive the test"
