"""Explicit synthetic measurement of the old status location's accounting eligibility."""

import json
import subprocess
import time
from types import ModuleType, SimpleNamespace

from quarterdeck import accounting_gate, presentation, service, status_snapshot, trusted_files, watch
from tests.unit.test_status_snapshot_ordinary import published


def test_old_status_location_reopens_a_completed_gate(published, monkeypatch, capsys):
    ship, _, _, _ = published
    before_source = subprocess.run(['git', 'show', '7a01c2e164103bc8b537c305b06c9145b7790962:src/quarterdeck/status_snapshot.py'],
                                   capture_output=True, text=True, check=True).stdout
    old = ModuleType('quarterdeck._measured_old_status_snapshot')
    old.__package__ = 'quarterdeck'
    exec(compile(before_source, '<old status snapshot>', 'exec'), old.__dict__)
    trusted_files.root(ship.runtime).mkdir(exist_ok=True)
    actor = service.Service(ship, SimpleNamespace())
    native_time = time.time_ns
    monkeypatch.setattr(accounting_gate, 'time', SimpleNamespace(
        time_ns=lambda: native_time() + 10 * accounting_gate.post.POST_TIMESTAMP_TICK_NS,
        monotonic=time.monotonic))
    gate = accounting_gate.Gate()
    try:
        actor.first_beat()
        reading = watch.status_reading(ship, actor.tmux)
        def publish():
            old.publish(ship, reading, wall=time.time_ns(), monotonic=time.monotonic_ns(),
                        framework=presentation.framework_stamp(), stamp=presentation.fingerprint(ship.file))
        samples = []
        for action, write in (('publication', publish), ('demand', lambda: old.request(ship)),
                              ('publication-replacement', publish), ('demand-replacement', lambda: old.request(ship))):
            gate.begin(actor, ())
            gate.finish(actor, actor.ledger.db.total_changes, full=True)
            assert gate.completed is not None and not gate.allowed(actor)
            before = accounting_gate.capture(actor, ())
            write()
            after = accounting_gate.capture(actor, ())
            allowed = gate.allowed(actor)
            changes = accounting_gate.differences(before, after)
            samples.append(dict(action=action, accounting_job_eligible=allowed, changed_classes=changes))
            assert allowed and changes == {'settings': 1}
        with capsys.disabled():
            print('STATUS_GATE_BEFORE ' + json.dumps(dict(source='7a01c2e1', samples=samples,
                                                         scope='synthetic Gate eligibility, not live jobs'), sort_keys=True), flush=True)
    finally:
        actor.ledger.close()
