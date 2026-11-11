"""Proved unsubmitted-paste cleanup and scoped negative evidence, never receipt/replay."""
import hashlib
import json
import re
import time
from pathlib import Path

from . import pending
from .harness.base import record_fingerprint
from .tmux import TmuxError


def record_binding(seat):
    path=seat.record()
    return {'path':str(path) if path else None,
            'stamp':json.loads(json.dumps(record_fingerprint(path))) if path else None}


def context(seat,text,screen):
    launch=seat.launched();pane=seat.pane()
    if (not launch or not launch.version or not pane or pane.dead or pane.in_mode
            or screen is None or screen.box!='' or screen.working or screen.interruption
            or screen.unknown or screen.starting):return None
    return dict(format=1,identity=pending.identity(seat),adapter=seat.harness.name,version=launch.version,
                geometry=[pane.width,pane.height],payload=hashlib.sha256(text.encode()).hexdigest(),
                frame=hashlib.sha256(screen.text.encode()).hexdigest(),record=record_binding(seat))


def refused(seat,text,screen):
    """A changed payload/version/launch/geometry/frame/record must be evaluated anew."""
    try:
        current=context(seat,text,screen)
        if current is None:return None
        proofs,errors=pending.journals(seat)
        if errors:return None  # ordinary pending hold handles unreadable evidence
        for proof in proofs:
            evidence=proof.data.get('paste_refusal')
            if (proof.data['phase']=='withheld' and proof.data['payload']==text and evidence
                    and evidence['context']==current):
                return evidence['reason']+'; unchanged proved paste/rendering conditions; no keys sent'
    except (OSError,ValueError,KeyError,TypeError,AttributeError,TmuxError):pass
    return None


def owner(door,text,screen):
    """In-process authority, acquired before our paste; never recovered from an old journal."""
    seat=door.seat;proof=door.proof
    try:
        if (seat.session.human or not proof or proof.data['phase']!='prepared'
                or proof.data['payload']!=text or not proof.same_launch(seat)
                or proof.data.get('recovery') or screen is None or screen.box!=''
                or screen.working or screen.unknown or screen.interruption):return None
        view=seat.tmux.session_view(seat.session.title)
        if view is None or view.scrolling or any(at>=int(door.since.timestamp()) for at in view.inputs):return None
        return dict(identity=pending.identity(seat),inputs=view.inputs,record=record_binding(seat))
    except (OSError,ValueError,KeyError,TypeError,AttributeError,TmuxError):return None


def clear(door,text,owned,failed,seconds):
    """Clear only fresh whole-payload evidence under the same unsubmitted owner."""
    seat=door.seat;proof=door.proof
    try:
        if not owned or seat.session.human or proof.data['phase']!='pasting' or not proof.same_launch(seat):return None
        screen=door.screen();pane=seat.pane();launch=seat.launched()
        if (not pane or not launch or pane.in_mode or pane.dead or screen is None or screen.working
                or screen.unknown or screen.interruption or screen.starting or not proof.same_launch(seat)
                or pending.identity(seat)!=owned['identity'] or record_binding(seat)!=owned['record']
                or proof.confirmed(seat) is not None):return None
        if owned['record']['path']:
            state=seat.harness.state(Path(owned['record']['path']))
            if (state.turn_open is not False or state.pending_tools is None or state.pending_tools
                    or state.queued or state.queue_error):return None
        view=seat.tmux.session_view(seat.session.title)
        if view is None or view.scrolling or view.inputs!=owned['inputs']:return None
        position=seat.tmux.editor_position(pane.id,pane.pid)
        if position is None or tuple(position[2:])!=(pane.width,pane.height):return None
        rule=seat.harness.paste_clear_rule(launch.version)
        if rule is None:return None
        keys=seat.harness.paste_clear_keys(text,screen,seat.harness.box_columns(pane.width),launch.version,position[:2])
        lines=len(text.split('\n'))
        if not keys or keys!=rule.keys(lines):return None
        confirmed=door.screen()
        if (confirmed is None or confirmed.text!=screen.text or not proof.same_launch(seat)
                or record_binding(seat)!=owned['record']):return None
        view=seat.tmux.session_view(seat.session.title)
        if view is None or view.scrolling or view.inputs!=owned['inputs']:return None
        intent=dict(format=1,identity=owned['identity'],position=list(position),
                    rule=rule.name,lines=lines,stage='clearing')
        proof.save(shown=failed.box if failed else None,paste_clear=intent)
        final=door.screen();view=seat.tmux.session_view(seat.session.title)
        if (final is None or final.text!=screen.text or not proof.same_launch(seat)
                or view is None or view.scrolling or view.inputs!=owned['inputs']
                or record_binding(seat)!=owned['record']):return None
        seat.tmux.clear_owned_paste(pane.id,pane.pid,position,keys)  # law: one-door
        intent={**intent,'stage':'checking_empty'}
        proof.save(paste_clear=intent)
        deadline=time.monotonic()+seconds
        while True:
            empty=door.screen()
            if not proof.same_launch(seat):return None
            if (empty is not None and empty.box=='' and not empty.working and not empty.unknown
                    and not empty.interruption and not empty.starting):break
            if time.monotonic()>=deadline:return None
            time.sleep(.05)
        if record_binding(seat)!=owned['record'] or proof.confirmed(seat) is not None:return None
        reading=seat.tmux.session_view(seat.session.title)
        if reading is None or reading.scrolling or reading.inputs!=owned['inputs']:return None
        observed='unreadable box' if failed is None or failed.box is None else f'{len(failed.box)} observed chars'
        reason=(f'whole-message paste check failed at {seat.harness.box_columns(pane.width)} columns '
                f'({len(text)} expected chars; {observed}); owned unsubmitted paste cleared and empty box confirmed')
        cache=context(seat,text,empty)
        if cache is None:return None
        proof.save(phase='withheld',shown=failed.box if failed else None,
                   paste_refusal=dict(format=1,context=cache,reason=reason),resolution=reason,
                   paste_clear={**intent,'stage':'empty_confirmed'})
        return reason
    except (OSError,ValueError,KeyError,TypeError,AttributeError,TmuxError):return None


def validate(evidence):
    if (not isinstance(evidence,dict) or evidence.get('format')!=1
            or not isinstance(evidence.get('reason'),str) or not evidence['reason'] or len(evidence['reason'])>256
            or not isinstance(evidence.get('context'),dict) or evidence['context'].get('format')!=1):
        raise ValueError('unknown paste refusal evidence')
    context=evidence['context']
    if (set(context)!={'format','identity','adapter','version','geometry','payload','frame','record'}
            or not isinstance(context['identity'],dict) or not isinstance(context['adapter'],str)
            or not isinstance(context['version'],str) or not context['version']
            or not isinstance(context['geometry'],list) or len(context['geometry'])!=2
            or any(type(n) is not int or n<1 for n in context['geometry'])
            or any(not isinstance(context[k],str) or re.fullmatch('[a-f0-9]{64}',context[k]) is None for k in ('payload','frame'))
            or not isinstance(context['record'],dict) or set(context['record'])!={'path','stamp'}):
        raise ValueError('unproved paste refusal context')


def validate_clear(intent,identity):
    from .harness import get
    if (not isinstance(intent,dict) or intent.get('format')!=1 or intent.get('identity')!=identity
            or not any(intent.get('rule')==rule.name for rule in get(identity['harness']).paste_clear_rules)
            or intent.get('stage') not in ('clearing','checking_empty','empty_confirmed')
            or type(intent.get('lines')) is not int or intent['lines']<1
            or not isinstance(intent.get('position'),list) or len(intent['position'])!=4
            or any(type(n) is not int or n<0 for n in intent['position'])):
        raise ValueError('unproved paste clear intent')
