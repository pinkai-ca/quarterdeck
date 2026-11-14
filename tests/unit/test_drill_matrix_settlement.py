"""Matrix reports can arrive at the deadline before their attempt is published."""

from types import SimpleNamespace

import pytest

from quarterdeck import drill, human_wake, post


def matrix(tmp_path, monkeypatch, *, settles=True, duplicate=False, result=None):
    exercise = drill.Drill.__new__(drill.Drill)
    exercise.store = post.Store(tmp_path)
    exercise.attempts = post.Attempts(tmp_path)
    exercise.ship = SimpleNamespace(
        seats={'one': None, 'two': None}, session_of=lambda _: SimpleNamespace(human=False))
    exercise.patience = 0.1
    exercise.checks = []
    exercise.seats = lambda: []
    attempts = []

    class Clock:
        elapsed = 0.0
        closed = False
        published = False

        def monotonic(self):
            return self.elapsed

        def sleep(self, seconds):
            self.elapsed += seconds
            orders = [message for message in exercise.store.all() if message.kind == 'order']
            if not attempts:
                for order in orders:
                    attempt = exercise.attempts.open(order, order.to, '%synthetic', 1, None)
                    attempts.append(attempt)
                    if result is not None:
                        exercise.attempts.close(attempt, result, 'synthetic unfinished receipt')
            # Reports become visible after the global wait's last predicate.
            if self.elapsed >= 0.2 and not self.closed:
                self.closed = True
                for order in orders:
                    exercise.store.new('report', order.to, order.sender, 'done', re=order.id)
            if settles and self.elapsed >= 0.25 and not self.published:
                self.published = True
                for attempt in attempts:
                    exercise.attempts.close(attempt, 'delivered', 'synthetic receipt published later')
                if duplicate:
                    extra = exercise.attempts.open(orders[0], orders[0].to, '%synthetic', 1, None)
                    exercise.attempts.close(extra, 'delivered', 'synthetic duplicate')

    clock = Clock()
    monkeypatch.setattr(drill, 'time', clock)
    return exercise, clock


@pytest.mark.parametrize('duplicate', [False, True])
def test_matrix_observes_late_attempt_after_report_at_global_deadline(tmp_path, monkeypatch, duplicate):
    exercise, clock = matrix(tmp_path, monkeypatch, duplicate=duplicate)
    exercise.matrix('synthetic')
    # Independently finish the publisher even if the old matrix has already returned.
    clock.sleep(0.1)
    assert all(attempt.result == 'delivered' for attempt in exercise.attempts.all())
    assert all(check.passed for check in exercise.checks) is not duplicate, '\n'.join(map(str, exercise.checks))
    assert all('closed by rep-' in check.detail for check in exercise.checks[:-1])


@pytest.mark.parametrize('result', [None, 'pending', 'blind'])
def test_matrix_receipt_timeout_is_bounded_and_names_actual_attempt_state(tmp_path, monkeypatch, result):
    exercise, clock = matrix(tmp_path, monkeypatch, settles=False, result=result)
    exercise.matrix('synthetic')
    assert clock.elapsed <= 0.300001
    assert [attempt.result for attempt in exercise.attempts.all()] == [result, result]
    for check in exercise.checks[:-1]:
        assert not check.passed
        assert f"attempts {[result or 'unfinished']}" in check.detail
        assert 'closed by rep-' in check.detail
    assert exercise.checks[-1].passed


@pytest.mark.parametrize('box,present,permitted', [
    ('', True, True),
    (human_wake.LINE, True, True),
    (human_wake.LINE + ' ', True, False),
    (human_wake.LINE + '\n', True, False),
    (human_wake.LINE + '\nordinary message', True, False),
    (' ' + human_wake.LINE, True, False),
    (human_wake.LINE[:-1], True, False),
    ('ordinary draft', True, False),
    (None, True, False),
    ('', False, False),
])
def test_matrix_human_box_allows_only_empty_or_exact_fixed_waking_line(tmp_path, box, present, permitted):
    screen = SimpleNamespace(box=box) if present else None
    seat = SimpleNamespace(name='desk', session=SimpleNamespace(human=True),
                           pane=lambda: SimpleNamespace(id='%synthetic'),
                           harness=SimpleNamespace(screen=lambda _: screen))
    exercise = drill.Drill(SimpleNamespace(runtime=tmp_path, seats={}),
                           SimpleNamespace(capture=lambda _: 'synthetic frame'), patience=0.1)
    exercise.seats = lambda: [seat]
    exercise.matrix('synthetic')
    check = exercise.checks[-1]
    assert check.name == 'synthetic: desk box has only permitted input'
    assert check.passed is permitted
