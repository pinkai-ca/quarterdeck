"""A synthetic rendezvous releases explicitly or fails within its bound."""

import pytest

from quarterdeck.harness.synthetic_control import Rendezvous, pause


def test_rendezvous_observes_only_its_input_and_releases_explicitly(tmp_path):
    seen = []
    with Rendezvous(tmp_path, "accept", "chosen") as gate:
        pause(tmp_path, "accept", "another input", lambda _: pytest.fail("unrelated input held"))
        assert gate.reached() is None
        def release(_):
            seen.append(gate.reached())
            gate.release()
        pause(tmp_path, "accept", "chosen exact input", release)
        assert len(seen) == 1 and seen[0]["text"] == "chosen exact input"
        assert seen[0]["phase"] == "accept"
    assert list((tmp_path / "rendezvous").iterdir()) == []


def test_an_unreleased_rendezvous_fails_instead_of_accepting_on_timeout(tmp_path):
    with pytest.raises(TimeoutError, match="was not released"):
        with Rendezvous(tmp_path, "accept", "chosen", seconds=0):
            pause(tmp_path, "accept", "chosen", lambda _: pytest.fail("deadline already passed"))
    assert list((tmp_path / "rendezvous").iterdir()) == []
