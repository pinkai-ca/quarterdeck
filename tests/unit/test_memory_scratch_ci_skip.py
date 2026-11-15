"""Memory-specific nested tests share their siblings' actual admission threshold."""

import os
from types import SimpleNamespace

import pytest

from tests.integration import test_testrun_memory_scratch as memory_tests
from tests.integration import testrun_synthetic as synthetic
from tests.testrunner import scratch as storage


@pytest.mark.parametrize('room', [0, storage.BOUND, storage.BOUND + storage.MARGIN - 1,
                                 storage.BOUND + storage.MARGIN, storage.BOUND + storage.MARGIN + 1])
def test_memory_module_and_ci_siblings_agree_on_small_and_sufficient_room(tmp_path, monkeypatch, room):
    checked, ran = [], []
    needed = storage.BOUND + storage.MARGIN

    def choices(bound=storage.BOUND, *, physical=False):
        checked.append((bound, physical))
        assert bound == needed and physical is True
        return iter([(tmp_path, 'tmpfs')]) if room >= bound else iter(())

    # Only these two skip checks see the model; no real reservation or filesystem is changed.
    monkeypatch.setattr(storage, 'choices', choices)
    monkeypatch.setattr(storage, 'os', SimpleNamespace(
        open=os.open, close=os.close,
        fstatvfs=lambda fd: SimpleNamespace(f_bavail=room, f_frsize=1)))
    sibling = synthetic.needs_ci_memory(lambda: ran.append('sibling'))
    module_guard = memory_tests.memory_reservation.__wrapped__
    assert memory_tests.skip_without_ci_memory is synthetic.skip_without_ci_memory
    if room < needed:
        for check in (sibling, module_guard):
            with pytest.raises(pytest.skip.Exception) as skipped:
                check()
            assert skipped.value.msg == storage.NO_CI_MEMORY
        assert not ran
    else:
        sibling()
        module_guard()
        assert ran == ['sibling']
    assert checked == [(needed, True), (needed, True)]


def test_memory_check_is_automatic_before_every_memory_case():
    assert memory_tests.memory_reservation._fixture_function_marker.autouse
