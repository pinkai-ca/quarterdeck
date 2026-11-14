"""Following starts with a fresh launch's first recorded turn."""
from types import SimpleNamespace
from quarterdeck.service import Service


def test_first_recorded_turn_follows_the_fresh_seat(tmp_path):
    record = [None]
    seat = SimpleNamespace(name='synthetic', session=SimpleNamespace(human=False),
        launched=lambda: object(), record=lambda: record[0],
        harness=SimpleNamespace(state=lambda _: SimpleNamespace(turn_open=True)))
    shown = []
    service = SimpleNamespace(working={}, work_marks={}, resolved_seats=lambda: [seat],
        show_window=lambda seat: shown.append(seat.name), log=lambda *args, **kwargs: None)
    service._working = lambda seat: Service._working(service, seat)
    Service.follow(service)
    assert service.working == {'synthetic': False}
    record[0] = tmp_path / 'synthetic.jsonl'
    record[0].write_text('{}\n')
    Service.follow(service)
    Service.follow(service)
    assert shown == ['synthetic']
