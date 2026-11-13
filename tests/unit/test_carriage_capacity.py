"""Synthetic ship-policy bounds for thread indexes and full self-handoffs."""
import json
from pathlib import Path

import pytest

from quarterdeck import carriage_plan as cp, doctor, ship
from quarterdeck.drill import Check
from quarterdeck.post_threads import IndexRow


def row(name='order-root', **changes):
    fields = dict(id=name, kind='order', sender='lead', subject='Synthetic subject', due='tomorrow', amendments=())
    fields.update(changes)
    return IndexRow(**fields)


def plan(rows=(), *, handoff='', limit=1800, **options):
    return cp.build(tuple(rows), handoff=handoff, header='qd > synthetic brief',
                    directory=Path('/synthetic/carried/current'), limit=limit, **options)


def index_rows(result):
    if 'index.txt' not in result.files:
        return [json.loads(line) for line in result.opening.splitlines() if line.startswith('{')]
    found = []
    def visit(name):
        for line in result.files[name].splitlines():
            data = json.loads(line)
            if 'first' in data:
                visit(Path(data['path']).name)
            else:
                found.append(data)
    visit('index.txt')
    return found


def test_small_index_inline_contains_due_amendments_and_whole_thread_instruction():
    # law: context-is-cache -- summaries never replace the complete immutable thread read
    result = plan([row(amendments=('answer-amendment',))])
    assert result.files == {}
    entry, = index_rows(result)
    assert entry['sender'] == 'lead' and entry['subject'] == 'Synthetic subject'
    assert entry['due'] == 'tomorrow' and entry['amendments'] == ['answer-amendment']
    assert entry['thread'] == 'qd thread order-root'
    assert 'read that thread whole' in result.opening
    assert 'policy bound' in result.opening
    assert cp.size(result.opening) <= 1800


def test_many_threads_form_complete_bounded_index_pages():
    rows = [row(f'order-item-{n:04}') for n in range(150)]
    result = plan(rows)
    assert cp.size(result.opening) <= 1800
    assert all(cp.size(text) <= 1800 for text in result.files.values())
    actual = index_rows(result)
    assert {entry['id'] for entry in actual} == {entry.id for entry in rows}
    assert len(actual) == 150
    assert len([name for name in result.files if name.startswith('threads-')]) > 2


def test_small_handoff_is_full_and_first_before_the_index():
    handoff = 'Invented complete self-handoff.\n' * 5
    result = plan([row()], handoff=handoff)
    assert handoff in result.opening
    assert result.opening.index(handoff) < result.opening.index('open threads:')
    assert result.files == {}


def test_large_handoff_is_one_complete_immutable_file_and_warns_honestly():
    handoff = 'Invented handoff 🦉é\n' * 2000
    result = plan([row()], handoff=handoff)
    assert result.files['handoff.txt'] == handoff
    assert cp.size(result.opening) <= result.limit
    assert all(cp.size(text) <= result.limit for name, text in result.files.items() if name != 'handoff.txt')
    assert f'your handoff is {cp.size(handoff)} bytes, over carry_bytes; write a shorter one next time' in result.opening
    assert result.opening.index(str(result.handoff_path)) < result.opening.index('open threads:')
    assert 'measured' not in result.opening
    assert not any(name.startswith(('view-', 'parts-')) for name in result.files)


def test_kernel_file_fallback_moves_even_a_small_handoff_out_of_argv():
    result = plan([row()], handoff='Small complete handoff.', force_files=True)
    assert result.files['handoff.txt'] == 'Small complete handoff.'
    assert 'Small complete handoff.' not in result.opening
    assert 'over carry_bytes' not in result.opening


def test_amendment_ids_too_large_for_one_line_have_complete_bounded_id_pages():
    ids = tuple(f'answer-item-{n:04}' for n in range(300))
    result = plan([row(amendments=ids)])
    entry, = index_rows(result)
    link = entry['amendments']
    assert link['count'] == len(ids)
    values = []
    def visit(name):
        for line in result.files[name].splitlines():
            data = json.loads(line)
            if 'first' in data:
                visit(Path(data['path']).name)
            else:
                values.append(data['value'])
    visit(Path(link['index']).name)
    assert values == list(ids)
    assert all(cp.size(text) <= result.limit for text in result.files.values())


def test_subject_summary_is_visible_and_json_escaped_on_one_line():
    result = plan([row(subject='Invented\n\t🦉"' * 500)])
    entry, = index_rows(result)
    assert entry['subject_truncated'] is True
    assert entry['subject'].endswith('...')
    assert entry['thread'] == 'qd thread order-root'
    assert len(json.dumps(entry['subject']).encode()) <= 200


def test_tiny_account_is_explicitly_impossible_before_publication(tmp_path):
    with pytest.raises(cp.ImpossibleBudget):
        cp.build((row(),), handoff='x' * 1000, header='header', directory=tmp_path / 'new', limit=30)
    assert list(tmp_path.iterdir()) == []


def test_indivisible_id_is_never_silently_cut():
    with pytest.raises(cp.ImpossibleBudget, match='minimal thread-index entry'):
        plan([row('long-identity-' * 500)])


def test_publish_is_private_complete_and_never_rewrites(tmp_path):
    result = cp.build(tuple(row(f'order-{n}') for n in range(20)), handoff='h' * 5000,
                      header='header', directory=tmp_path / 'generation', limit=1800)
    cp.publish(result)
    assert {p.name: p.read_text() for p in result.directory.iterdir()} == result.files
    assert result.directory.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in result.directory.iterdir())
    before = {p: p.read_bytes() for p in result.directory.iterdir()}
    with pytest.raises(FileExistsError):
        cp.publish(result)
    assert {p: p.read_bytes() for p in before} == before
    assert list(tmp_path.iterdir()) == [result.directory]


def test_failed_publication_exposes_no_partial_generation(tmp_path, monkeypatch):
    result = cp.build((row(),), handoff='h' * 5000, header='header', directory=tmp_path / 'generation', limit=1800)
    def fail(_folder):
        raise OSError('invented write interruption')
    monkeypatch.setattr(cp.durable, 'sync_folder', fail)
    with pytest.raises(OSError, match='invented write interruption'):
        cp.publish(result)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('value', [True, False, 0, -1, 1.5, '100000', None])
def test_ship_policy_rejects_non_positive_integer(value):
    with pytest.raises(ship.ShipError, match=r'ship\.carry_bytes'):
        ship._carry_bytes(value)


def test_ship_file_is_the_only_policy_source(tmp_path, monkeypatch):
    monkeypatch.setenv('QD_CARRY_BYTES', '999')
    assert ship._from_text('[ship]\n', tmp_path).carry_bytes == 100000
    assert ship._from_text('[ship]\ncarry_bytes = 1234\n', tmp_path).carry_bytes == 1234


def test_doctor_shows_policy_in_force_without_extra_program_observation(tmp_path, monkeypatch):
    declaration = ship.Ship(tmp_path, 'synthetic', 'operator', tmp_path, {}, {}, {}, carry_bytes=4321)
    monkeypatch.setattr(doctor, 'program_version', lambda _args: 'synthetic')
    monkeypatch.setattr(doctor, 'framework_check', lambda _ship: Check('framework', True, 'synthetic'))
    monkeypatch.setattr(doctor, 'program_path', lambda: '')
    monkeypatch.setattr(doctor.watch, 'disks', lambda _paths: [])
    found = doctor.checks(declaration)
    assert 'carry_bytes=4321 UTF-8 bytes' in found[0].detail
    assert 'policy bound' in found[0].detail
