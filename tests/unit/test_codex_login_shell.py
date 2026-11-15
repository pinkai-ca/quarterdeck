"""No Codex seat's commands read the operator's login startup files.

A seat inherits the operator's HOME. Codex would run its commands, and keep a shell snapshot it sources before each,
from a login shell, which reads that HOME's ~/.profile: on this machine that exported the sudo helper and defined a
sudo() function raising a password prompt on his screen. The harness writes allow_login_shell = false and
features.shell_snapshot = false into the seat's config, top level and every profile, whatever a template or the seat's
own settings say. Measured live on Codex 0.166.0 through qd's own throwaway path (tests/measure_codex_login_shell.py):
with both off, no snapshot is written and a command's environment holds neither the decoy name nor the helper.
"""
from dataclasses import replace
import tomllib

import pytest

from quarterdeck import harness, ship as shipmod


def spec(**changes):
    return replace(shipmod.SeatSpec("worker-example", "crew-example", "worker", "codex", ("pool-example",),
                                    model="model-example"), **changes)


def built(tmp_path, seat=None, template=None):
    ship_home = None
    if template is not None:
        ship_home = tmp_path / "ship-home"
        ship_home.mkdir()
        (ship_home / "config.toml").write_text(template)
    home = tmp_path / "runtime" / "seats" / "worker-example" / "home"
    harness.get("codex").build_home(home, seat or spec(), shipmod.Pool("pool-example", "openai"), ship_home)
    return tomllib.loads((home / "config.toml").read_text())


def scopes(config):
    return [config, *config.get("profiles", {}).values()]


def test_a_codex_seat_runs_no_login_shell_and_keeps_no_snapshot(tmp_path):
    config = built(tmp_path)
    assert config["allow_login_shell"] is False
    assert config["features"]["shell_snapshot"] is False
    assert config["features"]["memories"] is False, "the other features stay as they were"


TEMPLATE = '''
allow_login_shell = true
[features]
shell_snapshot = true
unified_exec = true
[profiles.example]
allow_login_shell = true
sandbox_mode = "read-only"
[profiles.example.features]
shell_snapshot = true
'''


@pytest.mark.parametrize("source", ["template", "seat settings"])
def test_neither_a_template_nor_a_seats_settings_turns_them_back_on(tmp_path, source):
    if source == "template":
        config = built(tmp_path, template=TEMPLATE)
    else:
        config = built(tmp_path, seat=spec(settings={"allow_login_shell": True, "features": {"shell_snapshot": True},
                                                     "profiles": {"example": {"allow_login_shell": True}}}))
    assert len(scopes(config)) == 2, "the profile is kept"
    for scope in scopes(config):
        assert scope["allow_login_shell"] is False
        assert scope["features"]["shell_snapshot"] is False
        assert scope["sandbox_mode"] == "danger-full-access"
    if source == "template":
        assert config["features"]["unified_exec"] is True, "a template's other features are kept"
