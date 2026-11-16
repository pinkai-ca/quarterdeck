"""Explicit, synthetic worker byte baseline captured before the hand-only change."""

import hashlib
from datetime import timedelta

from quarterdeck import post
from tests.unit.test_hand_open_context import AT, world


def test_worker_context_baseline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(post, 'now', lambda: AT + timedelta(seconds=30))
    w = world(tmp_path / 'ship', 'worker')
    inbox = post.inbox_text(w.store, w.seat.name)
    opening, _ = w.seat.first_message()
    with capsys.disabled():
        print('WORKER_CONTEXT_BASELINE inbox_sha256=' + hashlib.sha256(inbox.encode()).hexdigest()
              + ' opening_sha256=' + hashlib.sha256(opening.encode()).hexdigest(), flush=True)
