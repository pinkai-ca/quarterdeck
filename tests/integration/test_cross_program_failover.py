"""Private fake processes exercise the real Codex/Claude readers and fresh lifecycle."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from quarterdeck import cli, fresh_handoff, keyfile, post, quota, record_owner, watch
from quarterdeck.harness import resume_target
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.seat import Seat
from quarterdeck.service import Service
from quarterdeck.ship import Pool, SeatSpec, Session, Ship
from tests.unit.test_pool_choices import declaration
from .waiting import wait_for


def test_private_process_moves_fresh_with_full_messages_and_destination_program(tmp_path, tmux, monkeypatch, capsys):
    ship = declaration(tmp_path, '["first", {pool="second",harness="claude",model="synthetic-target",settings={step_seconds="0.05"}}]')
    # Program execution/configuration are synthetic; record and ownership adapters
    # remain the real ones. No provider credentials, programs or model calls.
    def launch(self, spec, pool, home, brief, resume, first):
        shaped = replace(spec, settings={**spec.settings, 'as': self.name, 'step_seconds': '0.05'})
        return FakeHarness().launch(shaped, pool, home, brief, resume, first)

    for adapter in (CodexHarness, ClaudeHarness):
        monkeypatch.setattr(adapter, 'launch', launch)
        monkeypatch.setattr(adapter, 'build_home', lambda self, *a: FakeHarness().build_home(*a))
        monkeypatch.setattr(adapter, 'screen', lambda self, *a: FakeHarness().screen(*a))
        monkeypatch.setattr(adapter, 'version', lambda self: 'synthetic')
        def no_provider(*args):
            raise AssertionError('private proof queried a provider')
        monkeypatch.setattr(adapter, 'usage', no_provider)
    monkeypatch.setattr(quota, 'refresh', lambda *a: None)
    monkeypatch.setattr(quota, 'refresh_declared', lambda *a: None)
    seat = Seat(ship, ship.seat('worker'), tmux)
    svc = Service(ship, tmux)
    initial = svc.store.new('order', 'issuer', 'worker', 'synthetic initial work')
    try:
        seat.up()
        def finished():
            record = seat.record()
            return record and seat.harness.state(record).turns and not seat.harness.state(record).turn_open
        wait_for(finished)
        svc.store.new('report', 'worker', 'issuer', 'synthetic initial work ended', re=initial.id)
        source, original_launch = seat.record(), seat.launched()
        before = source.read_bytes()
        order = svc.store.new('order', 'issuer', 'worker', 'full synthetic carried work\n' + 'exact payload '*60)
        question = svc.store.new('question', 'issuer', 'worker', 'full synthetic carried question')
        now = post.now()
        for name, used in (('first', 1.0), ('second', 0.0)):
            svc.ledger.record_usage(name, Usage((Window('5h', used, now+timedelta(hours=1)),), now))
        svc.account()
        svc.failover()
        current = Seat(ship, ship.seat('worker'), tmux)
        assert current.harness.name == 'claude' and current.pool.name == 'second'
        assert current.launched().resume is None and current.launched().pid != original_launch.pid
        assert '--resume' not in current.launched().argv
        first = current.launched().argv[current.launched().argv.index('--first')+1]
        rows = {row['id']: row for line in first.splitlines() if line.startswith('{')
                for row in [json.loads(line)]}
        for message in (order, question):
            assert rows[message.id]['thread'] == f'qd thread {message.id}'
        with monkeypatch.context() as caller:
            caller.setenv('QD_SHIP', str(ship.file.parent))
            caller.setenv('QD_SEAT', 'worker')
            capsys.readouterr()
            for message in (order, question):
                assert cli.main(['--ship', str(ship.file.parent), 'thread', message.id]) == 0
                printed = capsys.readouterr().out
                assert post.typed([message], whole=True) in printed
                assert message.body in printed
        brief = (ship.runtime/'brief/worker.md').read_text()
        assert '# Program: Claude Code' in brief and '# Program: Codex' not in brief
        wait_for(lambda: current.record() is not None)
        target = current.record()
        inputs = current.harness.inputs(target)
        assert any(first in entry.text for entry in inputs)
        assert source.read_bytes() == before and target != source
        assert {item.reader.name for item in record_owner.records(current)} == {'codex', 'claude'}
        wait_for(lambda: not current.harness.state(target).turn_open)
        svc.account()
        assert set(svc.ledger.db.execute('SELECT pool FROM turns')) == {('first',), ('second',)}
        assert watch.reading(current).harness == 'claude'
        assert 'worker on claude/second' in watch.facts(ship, tmux, walls=[], login=[]).line()
        previous = current.launched()
        for _ in range(2):
            svc.failover()
        assert current.launched() == previous
    finally:
        Seat(ship, ship.seat('worker'), tmux).down()
        svc.ledger.close()


class StandInKeyPool(Pool):
    kind = 'key'
    paid_by_key = True


@pytest.mark.parametrize('case', ['fresh', 'same-route', 'human', 'explicit'])
def test_provider_key_round_trip_with_stand_in(case, tmp_path, tmux, monkeypatch):
    from .waiting import start_with_conversation

    spec = SeatSpec('worker', 'crew', 'worker', 'fake', ('subscription', 'key'), settings={'step_seconds': '0.05'})
    key_provider = 'stand-in' if case in ('fresh', 'human') else 'synthetic'
    ship = Ship(tmp_path / 'ship', 'synthetic', 'operator', tmp_path,
                {'subscription': Pool('subscription', 'synthetic'), 'key': StandInKeyPool('key', key_provider)},
                {'crew': Session('crew', 'Synthetic')}, {'worker': spec})
    monkeypatch.setattr(FakeHarness, 'accepts', lambda self, pool: None)
    monkeypatch.setattr(keyfile, 'problem', lambda pool: None)
    monkeypatch.setattr(keyfile, 'route', lambda *args: None)
    monkeypatch.setattr(keyfile, 'value', lambda pool: pytest.fail('this stand-in uses no key'))
    real_resume = Seat._require_resume
    probes = []

    def resume(seat, spec=None, *, pool=None, expected=None):
        if pool is not None:
            probes.append((seat.pool.name, pool))
            if case in ('fresh', 'human'):
                raise resume_target.Unproved(seat.name, 'the synthetic conversation has a different route',
                                             launch=seat.launched(), pool=pool)
        return real_resume(seat, spec, pool=pool, expected=expected)

    monkeypatch.setattr(Seat, '_require_resume', resume)
    seat = Seat(ship, spec, tmux)
    svc = Service(ship, tmux)

    def usage(used):
        now = post.now()
        svc.ledger.record_usage('subscription', Usage((Window('5h', used, now + timedelta(hours=1)),), now))

    def idle_seat():
        current = Seat(ship, spec, tmux)
        return current if current.is_up and svc.not_idle_at_wall(current) is None else None

    try:
        if case == 'explicit':
            seat.up(resume=False, pool='key')
            assert seat.launched().pool_selected == 'key'
            usage(0)
            for _ in range(5):
                svc.failover()
            assert Seat(ship, spec, tmux).launched() == seat.launched() and not probes
            assert watch.service_holds(ship.runtime) == []
            return

        start_with_conversation(seat)
        source = wait_for(idle_seat)
        original_record, original_launch = source.record(), source.launched()
        handoff = svc.store.new('order', spec.name, spec.name, 'Keep the complete synthetic handoff.')
        work = svc.store.new('order', 'issuer', spec.name, 'Synthetic unfinished work')
        usage(1)
        svc.failover()
        away = wait_for(idle_seat)
        away_launch = away.launched()
        assert away.pool.name == 'key' and away_launch.at != original_launch.at
        assert probes == [('subscription', 'key')]
        if case == 'same-route':
            assert away_launch.resume and away.record() == original_record
        else:
            assert away_launch.resume is None and away.record() != original_record
            opening = away_launch.argv[away_launch.argv.index('--first') + 1]
            assert handoff.body in opening and work.id in opening

        if case == 'human':
            ship.sessions['crew'] = replace(ship.sessions['crew'], human=True)
        usage(0)
        for _ in range(5):
            svc.failover()
        assert probes == [('subscription', 'key'), ('key', 'subscription')]
        current = Seat(ship, spec, tmux)
        reports = [m.body for m in svc.store.for_seat('worker') if m.kind == 'report']
        if case == 'fresh':
            [ask] = [m for m in svc.store.for_seat(spec.name) if m.subject == fresh_handoff.SUBJECT]
            assert current.launched() == away_launch and current.pool.name == 'key'
            assert not any('moved back to pool' in report for report in reports)
            handoff = svc.store.new('order', spec.name, spec.name, 'Carry these new notes from the synthetic key.',
                                    re=ask.id)
            svc.store.new('report', spec.name, post.SHIP, f'Handoff: {handoff.id}', re=ask.id)

            for message in svc.attempts.undelivered(svc.store, spec.name):
                if message.id != handoff.id:
                    result = svc.deliver(wait_for(idle_seat), [message])
                    assert 'delivered' in result, result
            wait_for(idle_seat)
            svc.fresh_handoffs()
            current = Seat(ship, spec, tmux)
            reports = [m.body for m in svc.store.for_seat('worker') if m.kind == 'report']
        if case == 'human':
            assert current.launched() == away_launch and current.pool.name == 'key'
            assert not any('moved back to pool' in report for report in reports)
            line = watch.facts(ship, tmux, walls=[], login=[]).line()
            assert 'worker: return to subscription held: conversation resume unproved; needs the desk' in line
            usage(1)
            svc.failover()
            assert watch.service_holds(ship.runtime) == []
            return

        assert current.pool.name == 'subscription' and current.launched().at != away_launch.at
        assert sum('moved back to pool subscription from its key key: the subscription has room' in report
                   for report in reports) == 1
        assert sum('moved to pool key from subscription at its wall' in report for report in reports) == 1
        if case == 'same-route':
            assert current.launched().resume and current.record() == original_record
        else:
            assert current.launched().resume is None
            opening = current.launched().argv[current.launched().argv.index('--first') + 1]
            assert handoff.body in opening and work.id in opening
            assert sum('started a new conversation because the synthetic conversation has a different route.'
                       in report for report in reports) == 2
        assert watch.service_holds(ship.runtime) == []
    finally:
        Seat(ship, spec, tmux).down()
        svc.ledger.close()
