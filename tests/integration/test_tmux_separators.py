"""A tmux client without a UTF-8 locale prints a format's tabs as "_"; qd's every format keeps them (-u).

Reproduced on the test's own private server: the condition, a client whose locale is not UTF-8, and qd under it.
"""

import os
import subprocess

import pytest

from quarterdeck import tmux as tmuxmod

NO_UTF8 = {"LANG": "C", "LC_ALL": "C", "LC_CTYPE": "C"}


@pytest.fixture
def no_utf8_locale(monkeypatch):
    for name, value in NO_UTF8.items():
        monkeypatch.setenv(name, value)


def bare(server, *args, flag=()):
    """A tmux client on the same private server, as qd would run one, without qd's own address options."""
    command = ["tmux", *flag, "-S", server.socket, "-N", *args]
    env = dict(os.environ, **NO_UTF8)
    return subprocess.run(command, capture_output=True, text=True, env=env, check=True).stdout


def test_the_condition_reproduced_a_client_without_utf8_rewrites_tabs_and_u_keeps_them(tmux, tmp_path):
    tmux.new_session("separators")
    line = bare(tmux, "display-message", "-p", "-t", "separators", "#{session_name}\t#{pane_id}")
    assert "\t" not in line and line.startswith("separators_%"), "before: the tab printed as an underscore"
    kept = bare(tmux, "display-message", "-p", "-t", "separators", "#{session_name}\t#{pane_id}", flag=("-u",))
    assert "\t" in kept and kept.split("\t")[0] == "separators", "with -u the tab stands"


def test_qd_opens_reads_and_lists_panes_under_a_client_locale_without_utf8(tmux, tmp_path, no_utf8_locale):
    tmux.new_session("separators")
    pane = tmux.new_window("separators", "probe", tmp_path, ["sleep", "60"])
    assert pane.id.startswith("%") and pane.session == "separators" and pane.window == "probe" and pane.pid > 0
    assert tmux.pane(pane.id) == pane or tmux.pane(pane.id).pid == pane.pid
    read = tmux.read_pane(pane.id)
    assert read is not None and read[0].id == pane.id
    assert any(p.id == pane.id for p in tmux.panes("separators"))


@pytest.mark.parametrize("line", ["%1_Wrap_display_2676778_80_24_0_0_", "", "%1\tonly\tthree", "%1\ts\tw\tpid\t80\t24\t0\t0\t"])
def test_a_line_not_in_the_pane_format_is_a_named_error_that_shows_it(line):
    with pytest.raises(tmuxmod.TmuxFormatError) as raised:
        tmuxmod._parse(line)
    assert repr(line) in str(raised.value) and isinstance(raised.value, tmuxmod.TmuxError)
