"""The lifecycle wait retries acquisition only and preserves its finite deadline."""
import pytest

from quarterdeck import lifecycle


def test_busy_wait_expires_at_one_deadline_and_releases_its_descriptor(tmp_path, monkeypatch):
    clock = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(lifecycle.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(lifecycle.time, 'sleep', sleep)
    path = tmp_path / 'locks/lifecycle/worker.lock'
    with lifecycle.claim(tmp_path, 'worker'):
        before = path.stat()
        with pytest.raises(lifecycle.Busy) as caught:
            with lifecycle.claim(tmp_path, 'worker', wait=0.12):
                pytest.fail('a busy claim entered its effect')
        assert str(caught.value) == 'worker: another lifecycle transition is in progress; retry when it ends'
        assert clock[0] == pytest.approx(0.12)
        assert sleeps == pytest.approx([0.05, 0.05, 0.02])
        assert path.stat().st_ino == before.st_ino
    with lifecycle.claim(tmp_path, 'worker', wait=0.12):
        assert path.stat().st_ino == before.st_ino
    assert clock[0] == pytest.approx(0.12), 'an uncontended claim never sleeps'


def test_default_claim_and_another_seat_remain_nonblocking(tmp_path, monkeypatch):
    def no_sleep(seconds):
        pytest.fail('background claim waited')

    monkeypatch.setattr(lifecycle.time, 'sleep', no_sleep)
    with lifecycle.claim(tmp_path, 'worker'):
        with pytest.raises(lifecycle.Busy):
            with lifecycle.claim(tmp_path, 'worker'):
                pytest.fail('a busy claim entered its effect')
        with lifecycle.claim(tmp_path, 'other'):
            pass


def test_explicit_scope_is_nested_and_restored_after_failure():
    assert lifecycle.explicit_wait() == 0
    with lifecycle.explicit():
        assert lifecycle.explicit_wait() == lifecycle.WAIT_SECONDS
        with pytest.raises(ValueError):
            with lifecycle.explicit():
                raise ValueError('synthetic failed command')
        assert lifecycle.explicit_wait() == lifecycle.WAIT_SECONDS
    assert lifecycle.explicit_wait() == 0


def test_explicit_scope_does_not_make_automatic_or_other_thread_claims_wait(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from quarterdeck.seat import Seat

    seat = Seat.__new__(Seat)
    seat.ship = SimpleNamespace(runtime=tmp_path)
    seat.spec = SimpleNamespace(name='worker')
    monkeypatch.setattr(lifecycle.time, 'sleep', lambda seconds: pytest.fail('automatic claim waited'))
    with lifecycle.claim(tmp_path, 'worker'), lifecycle.explicit():
        with pytest.raises(lifecycle.Busy):
            with seat._lifecycle(automatic=True):
                pytest.fail('automatic claim entered a busy transition')
        with ThreadPoolExecutor(max_workers=1) as threads:
            assert threads.submit(lifecycle.explicit_wait).result(timeout=10) == 0
