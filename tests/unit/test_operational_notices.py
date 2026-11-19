import json
import os
from dataclasses import replace
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from quarterdeck import accounting_health, operational_notices as notices, presentation, service_status
from quarterdeck import servicectl, ship, watch
from quarterdeck.tmux import Tmux


FIRST = 'backup waiting for local disk'
SECOND = 'index rebuilding 4 files'


def notice(text):
    pid = os.getpid()
    wall = time.time_ns()
    return dict(format=1, text=text, pid=pid, process_start=notices.writer_start(pid),
                wall_ns=wall, monotonic_ns=time.monotonic_ns(), expires_ns=wall + 60_000_000_000)


def put(folder, tool, **data):
    path = folder / f'{tool}.json'
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


@pytest.fixture
def evidence(tmp_path):
    (tmp_path / 'ship.toml').write_text('[ship]\nname="synthetic"\ncode="checkout"\n')
    declaration = ship.load(tmp_path)
    folder = declaration.runtime / notices.DIRECTORY
    folder.mkdir(mode=0o700, parents=True)
    return declaration, folder


def foreign_owner(path, monkeypatch):
    target = path.stat()
    fstat = os.fstat

    def foreign(fd):
        info = fstat(fd)
        if (info.st_dev, info.st_ino) != (target.st_dev, target.st_ino):
            return info
        return SimpleNamespace(st_uid=info.st_uid + 1, st_mode=info.st_mode, st_size=info.st_size)

    monkeypatch.setattr(os, 'fstat', foreign)


def test_two_tools_keep_lines_on_overflow_and_omit_expired_and_foreign(evidence, monkeypatch):
    declaration, folder = evidence
    before = service_status.inputs(declaration)
    assert str(folder) in dict(before)
    put(folder, 'index', **notice(SECOND))
    backup = put(folder, 'backup', **notice(FIRST))
    expired = notice('outdated')
    for key in ('wall_ns', 'monotonic_ns', 'expires_ns'):
        expired[key] -= notices.MAX_TTL_NS
    put(folder, 'expired', **expired)
    foreign_owner(put(folder, 'foreign', **notice('foreign file')), monkeypatch)
    assert service_status.inputs(declaration) != before
    content, stamp = backup.read_bytes(), backup.stat().st_mtime_ns
    assert notices.readings(declaration.runtime) == [FIRST, SECOND]
    assert backup.read_bytes() == content and backup.stat().st_mtime_ns == stamp
    facts = watch.facts(declaration, SimpleNamespace())
    assert facts.operational_notices == [FIRST, SECOND]
    assert f'{FIRST} · {SECOND}' in facts.line()
    overflow = watch.Facts('ok', 0, 0, 0, operational_notices=[FIRST, SECOND],
                           needs_human=['worker' * 40]).line()
    assert f'{FIRST} · {SECOND}' in overflow
    assert '1 needs a human' in overflow and overflow.endswith(' · qd status')


def test_maximum_notices_share_one_display_budget_after_required_alerts():
    facts = watch.Facts('ok', 2, 1, 1, needs_human=['ops'], held=['worker'],
                        pending=['post withheld'], disks=[('data', 95)])
    required = facts.line()
    line = replace(facts, operational_notices=['a' * notices.MAX_TEXT_BYTES, 'b' * notices.MAX_TEXT_BYTES]).line()
    assert line == required.removesuffix(' · qd status') + ' · 2 tool notices · qd status'
    window = line[:160]
    for alert in ('service ok', 'ops needs a human', 'post withheld', '1 blind', 'worker held at limit',
                  'disk: data 95%', '2 tool notices'):
        assert alert in window


def test_notices_follow_required_categories_in_ordinary_overflow():
    facts = watch.Facts('down', 1, 2, 1, needs_human=['ops' * 60], held=['worker'],
                        pending=['post withheld'], disks=[('data', 95)])
    line = replace(facts, operational_notices=['a' * notices.MAX_TEXT_BYTES, 'b' * notices.MAX_TEXT_BYTES]).line()
    assert line == facts.line().removesuffix(' · qd status') + ' · 2 tool notices · qd status'
    assert line.index('disk: data 95%') < line.index('1 waiting') < line.index('2 tool notices')


def test_oversized_detail_is_counted_and_still_available_in_full_status():
    facts = watch.Facts('down', 1, 2, 1, needs_human=['ops'], held=['worker'],
                        pending=['post withheld'], disks=[('data', 95)], ledger_recovery=['ledger ' + 'x' * 160])
    line = replace(facts, operational_notices=[FIRST]).line()
    from quarterdeck.headlines import columns, details
    assert columns(line) <= 160 and line.endswith(' · qd status')
    assert '+1 more' in line and facts.ledger_recovery[0] in details(facts)


def test_single_notice_uses_display_columns_and_keeps_literal_text_when_it_fits():
    facts = watch.Facts('ok', 0, 0, 0, needs_human=['worker-' + 'x' * 55])
    required = facts.line()
    assert replace(facts, operational_notices=[FIRST]).line() == (
        required.removesuffix(' · qd status') + ' · ' + FIRST + ' · qd status')
    wide = '界' * 42
    assert len(required + ' · ' + wide) < 160 < len(required + ' · ') + 2 * len(wide)
    assert replace(facts, operational_notices=[wide]).line() == (
        required.removesuffix(' · 0 waiting · qd status') + ' · 1 tool notice · qd status')


@pytest.mark.parametrize('which', ['folder', 'writer'])
def test_foreign_directory_or_writer_is_omitted(evidence, monkeypatch, which):
    declaration, folder = evidence
    data = notice(FIRST)
    put(folder, 'backup', **data)
    assert notices.readings(declaration.runtime) == [FIRST]
    foreign_owner(folder if which == 'folder' else Path(f'/proc/{data["pid"]}'), monkeypatch)
    assert notices.readings(declaration.runtime) == []


@pytest.mark.parametrize('token', ('#[', '#{', '#(', '##', '#}', '#,', '#:', *('#' + c for c in 'DFHIPSTWh')))
def test_text_rejects_tmux_format_introducers(token):
    assert not notices._valid_text('index waiting ' + token + ' here')


@pytest.mark.parametrize('text', ('issue #123 is waiting', 'hash #b7abc89', 'ticket # and index 92%', '#'))
def test_text_keeps_literal_hashes_and_issue_numbers(text):
    assert notices._valid_text(text)


@pytest.mark.parametrize('failure', [
    'future', 'clocks disagree', 'prior process', 'not running', 'boolean pid', 'long lifetime', 'no expiry',
    'length', 'empty', 'untrimmed', 'multiline', 'control', 'format control', 'tmux styling', 'unexpected field',
    'malformed', 'duplicate fields', 'oversize file', 'writable file', 'fifo', 'link', 'subdirectory', 'bad name',
])
def test_bad_notice_is_omitted_without_discarding_valid_peer(evidence, failure, tmp_path):
    declaration, folder = evidence
    put(folder, 'index', **notice(SECOND))
    data = notice(FIRST)
    if failure == 'future':
        data['wall_ns'] += notices.MAX_TTL_NS
    elif failure == 'clocks disagree':
        data['monotonic_ns'] -= 2 * notices.CLOCK_SKEW_NS
    elif failure == 'prior process':
        data['process_start'] = str(int(data['process_start']) + 1)
    elif failure == 'not running':
        data['pid'] = 2**31 - 1
    elif failure == 'boolean pid':
        data['pid'] = True
    elif failure == 'long lifetime':
        data['expires_ns'] = data['wall_ns'] + notices.MAX_TTL_NS + 1
    elif failure == 'no expiry':
        del data['expires_ns']
    elif failure == 'unexpected field':
        data['count'] = 4
    elif failure == 'bad name':
        put(folder, 'bad.name', **data)
    elif failure in ('length', 'empty', 'untrimmed', 'multiline', 'control', 'format control', 'tmux styling'):
        data['text'] = {
            'length': '界' * 43, 'empty': '', 'untrimmed': ' leading space', 'multiline': 'one\ntwo',
            'control': 'a\x1bb', 'format control': 'a\u202eb', 'tmux styling': 'a #[reverse]b',
        }[failure]
    if failure != 'bad name':
        path = put(folder, 'backup', **data)
        if failure == 'fifo':
            path.unlink()
            os.mkfifo(path)
        elif failure == 'link':
            target = tmp_path / 'outside'
            path.rename(target)
            path.symlink_to(target)
        elif failure == 'subdirectory':
            path.unlink()
            path.mkdir()
        elif failure == 'writable file':
            path.chmod(0o620)
        elif failure in ('malformed', 'duplicate fields', 'oversize file'):
            path.write_text({
                'malformed': '{', 'duplicate fields': '{"format":1,' + json.dumps(data)[1:],
                'oversize file': ' ' * (notices.MAX_BYTES + 1),
            }[failure])
    assert notices.readings(declaration.runtime) == [SECOND]


@pytest.mark.parametrize('which', ['directory link', 'overfull', 'writeable directory', 'absent'])
def test_unusable_directory_is_empty(evidence, tmp_path, which):
    declaration, folder = evidence
    if which == 'absent':
        folder.rmdir()
    else:
        put(folder, 'backup', **notice(FIRST))
        if which == 'directory link':
            target = tmp_path / 'outside'
            folder.rename(target)
            folder.symlink_to(target, target_is_directory=True)
        elif which == 'overfull':
            for number in range(notices.MAX_ENTRIES):
                (folder / f'.{number}').touch()
        else:
            folder.chmod(0o770)
    assert notices.readings(declaration.runtime) == []


@pytest.mark.parametrize('clear', ['remove', 'change', 'expire', 'restart'])
@pytest.mark.parametrize('published_age', [0, 91])
def test_publication_cannot_repeat_a_superseded_notice(evidence, monkeypatch, clear, published_age):
    declaration, folder = evidence
    data = notice(FIRST)
    path = put(folder, 'backup', **data)
    line = watch.Facts('ok', 0, 0, 0, operational_notices=[FIRST]).line()
    now = time.time_ns()
    publication = dict(format=1, wall=now, written=now, monotonic=time.monotonic_ns(),
                       pid=data['pid'], process_start=data['process_start'], line=line, operational_notices=[FIRST],
                       ship=presentation.fingerprint(declaration.file), framework=presentation.framework_stamp(),
                       accounting=None, ledger_recovery=[], retirement=None)
    (declaration.runtime / presentation.NAME).write_text(json.dumps(publication))
    monkeypatch.setattr(servicectl, 'read_heartbeat', lambda runtime: (data['pid'], 0))
    monkeypatch.setattr(servicectl, 'alive', lambda runtime: True)
    monkeypatch.setattr(accounting_health, 'reading', lambda runtime: None)
    monkeypatch.setattr(presentation, 'service_lead', lambda runtime: 'service ok')
    assert presentation.cached_line(declaration.folder) == line
    monkeypatch.setattr(presentation, 'fresh', lambda *args: False)  # also exercise the longer-lived fallback
    assert presentation.published_reading(declaration.folder, status_refresh=30) == (line, None)
    if clear == 'remove':
        path.unlink()
    else:
        if clear == 'change':
            data['text'] = 'backup rebuilding'
        elif clear == 'expire':
            for key in ('wall_ns', 'monotonic_ns', 'expires_ns'):
                data[key] -= notices.MAX_TTL_NS
        else:
            data['process_start'] = str(int(data['process_start']) + 1)
        put(folder, 'backup', **data)
    assert presentation.cached_line(declaration.folder) is None
    publication['written'] = now - published_age * 1_000_000_000
    (declaration.runtime / presentation.NAME).write_text(json.dumps(publication))
    monkeypatch.setattr(presentation.time, 'time_ns', lambda: now)
    assert presentation.published_reading(declaration.folder, status_refresh=30) == (None, published_age or None)
    monkeypatch.setattr(Tmux, 'for_ship', classmethod(lambda cls, ship: SimpleNamespace()))
    text = presentation.command_line(['--ship', str(declaration.folder), 'status', '--line'])
    assert FIRST not in text
    assert ('publication 91s old, collected here' in text) == bool(published_age)
    if clear == 'change':
        assert 'backup rebuilding' in text
