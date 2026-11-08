"""Synthetic -J captures: retained right padding is not a foreign draft."""

from types import SimpleNamespace

import pytest

from quarterdeck.door import Door
from quarterdeck.harness import get

NAMES = ('codex', 'claude', 'antigravity', 'fake', 'opencode', 'pi')


def capture(name, body, padding=0, width=80):
    def pad(line):
        return line.ljust(width) if padding == 'full' else line + ' '*padding
    rows = body.split('\n')
    if name == 'opencode':
        lines = ['  ┃', *['  ┃  ' + r for r in rows], '  ┃', '  ┃  Build auto · TestModel pool', '  ╹' + '▀'*(width-3), '  /work   ctrl+p commands']
    elif name == 'pi':
        lines = ['─'*width, *rows, '─'*width, '/work', '0.0%/98k (auto)  TestModel • low']
    else:
        prompt = {'codex':'› ', 'claude':'❯ ', 'antigravity':'> ', 'fake':'> '}[name]
        content = [prompt+rows[0], *['  '+r for r in rows[1:]]]
        if name == 'codex':
            lines = [*content, '', '  TestModel low · /work', '  ? for shortcuts']
        else:
            status = {'claude':'  ○ low · /effort', 'antigravity':'  TestModel · low',
                      'fake':'fake claude · model TestModel · effort low · idle'}[name]
            lines = ['─'*60, *content, '─'*60, status]
    return '\n'.join(map(pad, lines))+'\n'


def door_for(name, text, monkeypatch, width=80):
    # These are layout contracts, with no pool/session/provider state to inspect.
    monkeypatch.setattr(Door, 'wall', lambda self, screen: None)
    seat = SimpleNamespace(harness=get(name), session=SimpleNamespace(human=False), spec=SimpleNamespace(),
                           pane=lambda:SimpleNamespace(id='%1', width=width, dead=False),
                           tmux=SimpleNamespace(capture=lambda *a,**k:text))
    return Door(seat)


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('padding',[3,4,'full'])
def test_empty_padded_prompt_rows_open_every_adapter(name, padding, monkeypatch):
    door = door_for(name,capture(name,'\n',padding),monkeypatch)
    assert door.screen().box == ''
    assert door.reading().open  # law: one-door


@pytest.mark.parametrize('name,placeholder',[('codex','Ask Codex to do anything'),
    ('claude','Press up to edit queued messages'),('antigravity','Press up to edit queued messages'),
    ('opencode','Ask anything… "What is the tech stack of this project?"')])
@pytest.mark.parametrize('padding',[3,4,'full'])
def test_recognized_placeholder_padding_is_empty_not_an_obstruction(name, placeholder, padding, monkeypatch):
    door = door_for(name,capture(name,placeholder,padding),monkeypatch)
    assert door.screen().box == '' and door.reading().open
    assert not door_for(name,capture(name,placeholder+' edited',padding),monkeypatch).reading().open


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('padding',[3,4,'full'])
def test_nonempty_padding_verifies_only_known_payload_and_keeps_drafts_obstructed(name, padding, monkeypatch):
    payload = 'keep  both spaces\n\n  preserve indentation\nlast line'
    door = door_for(name,capture(name,payload,padding),monkeypatch)
    screen = door.screen()
    assert screen.box.endswith(' '), 'keep raw nonempty padding for ownership checks'
    assert not door.reading().open and door.reading().clears == 'needs a human'
    assert door.matches(get(name).box(payload+'\n'),screen)
    assert not get(name).box(payload).matches(screen.box), 'no layout evidence, no unpadding'
    for edited in (payload.replace('  both',' both'),payload.replace('\n\n','\n'),payload[:-1],
                   payload.replace('spaces\n','spaces \n'),payload.replace('  preserve',' preserve')):
        assert not door.matches(get(name).box(edited),screen)


@pytest.mark.parametrize('name', NAMES)
def test_padding_does_not_remove_hyphens_or_short_hard_newlines(name, monkeypatch):
    width = 40
    columns = get(name).box_columns(width)
    first = 'x'*(columns-6)+'fresh-'
    door = door_for(name,capture(name,first+'\nlaunch', 'full', width),monkeypatch,width)
    assert door.matches(get(name).box(first+'launch'),door.screen())
    assert not door.matches(get(name).box(first.replace('fresh-', 'fresh -')+'launch'),door.screen())
    hard = door_for(name,capture(name,'short\nhard break','full',width),monkeypatch,width)
    assert hard.matches(get(name).box('short\nhard break'),hard.screen())
    assert not hard.matches(get(name).box('shorthard break'),hard.screen())
    assert not hard.matches(get(name).box('short hard break'),hard.screen())


def test_unsupported_or_ambiguous_padding_stays_unverified():
    from quarterdeck.composer import matches
    assert not matches('text ', 'text'+' '*20, 40)
    assert not matches('text \nnext','text'+' '*20+'\nnext'+' '*20,40)
    assert not matches('text','text'+' '*100,40)
    assert not matches('text','text\t',40)


def test_padding_that_hides_a_text_difference_cannot_confirm_delivery(tmp_path):
    from datetime import datetime, timedelta, timezone
    import json
    from quarterdeck.composer import matches
    from quarterdeck.input_receipt import confirmed

    stamp = datetime(2025,1,1,tzinfo=timezone.utc)
    record = tmp_path/'synthetic.jsonl'
    wanted = 'keep  interior spaces\nlast line'
    shown = '\n'.join(line.ljust(38) for line in wanted.split('\n'))
    assert matches(wanted,shown,38), 'visual verification tolerates painted padding'
    # A terminal cannot distinguish these spaces from painted right padding.
    # It still cannot establish delivery: exact, timestamped input is required.
    for actual in ('keep  interior spaces  \nlast line', 'keep interior spaces\nlast line'):
        row = {'type':'response_item','timestamp':stamp.isoformat(),
               'payload':{'type':'message','role':'user','content':[{'type':'input_text','text':actual}]}}
        record.write_text(json.dumps(row)+'\n')
        assert confirmed('codex',record,wanted,stamp-timedelta(seconds=1)) is None  # law: record-first
    row['payload']['content'][0]['text'] = wanted
    record.write_text(json.dumps(row)+'\n')
    assert confirmed('codex',record,wanted,stamp-timedelta(seconds=1)) == 'delivered'
