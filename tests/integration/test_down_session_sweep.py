"""The empty-session sweep on one private server shared by two synthetic ships."""

from quarterdeck import seat, ship


def test_down_sweeps_only_empty_sessions_owned_by_its_ship(tmux, tmp_path, monkeypatch):
    shared = ship.Session("crew", "Shared Crew")
    own = ship.Session("empty", "Own Empty")
    first = ship.Ship(tmp_path / "first", "first", "test", tmp_path, {},
                      {"crew": shared, "empty": own}, {})
    second = ship.Ship(tmp_path / "second", "second", "test", tmp_path, {},
                       {"crew": shared}, {})
    tmux.new_session(shared.title, env={"QD_SHIP": str(second.folder)})
    tmux.new_session(own.title, env={"QD_SHIP": str(first.folder)})

    # Native tmux removes a session with its last pane. Supply only that empty
    # reading; session discovery, ownership reads and closing use the real server.
    monkeypatch.setattr(tmux, "panes", lambda session=None: [])

    assert seat.bring_down(first, tmux) == []
    assert not tmux.has_session(own.title)
    assert tmux.has_session(shared.title)
    assert tmux.session_environment(shared.title, "QD_SHIP") == str(second.folder)

    assert seat.bring_down(second, tmux) == []
    assert not tmux.has_session(shared.title)
