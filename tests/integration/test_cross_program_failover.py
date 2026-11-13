"""Private fake processes exercise the real Codex/Claude readers and fresh lifecycle."""

from dataclasses import replace
from datetime import timedelta
import json

from quarterdeck import cli, post, quota, record_owner, watch
from quarterdeck.harness.claude import ClaudeHarness
from quarterdeck.harness.codex import CodexHarness
from quarterdeck.harness.fake import FakeHarness
from quarterdeck.harness.limits import Usage, Window
from quarterdeck.seat import Seat
from quarterdeck.service import Service
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
