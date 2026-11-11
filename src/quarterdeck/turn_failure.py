"""Provider-error visibility and operational metadata, never a lifecycle or replay policy."""
from datetime import datetime
import json
from pathlib import Path

from . import durable,post
from .harness.base import record_fingerprint
from .harness.failures import words
from .harness.types import TurnFailure

FILE='turn-failure.json'


def identity(seat):
    launched=seat.launched()
    return ({'pane':launched.pane,'pid':launched.pid,'at':launched.at,'harness':seat.harness.name}
            if launched else None)


def publish(seat,record,state,turns,stamp=None):
    """Service-only projection: no inputs, replies, subjects, usage or work summaries."""
    stamp=stamp if stamp is not None else record_fingerprint(record)
    if record_fingerprint(record)!=stamp:
        return  # never certify old turn data against a newer source fingerprint
    failure=getattr(state,'failure',None)
    current=next((t for t in reversed(turns) if t.failure==failure),None) if failure else None
    data={'format':1,'identity':identity(seat),'record':str(record),
          'fingerprint':stamp,'failure':None}
    if failure:
        data['failure']={'message':words(failure.message),'kind':failure.kind,
                         'model_output':failure.model_output,'at':failure.at.isoformat() if failure.at else None,
                         'turn':current.index if current else None}
    path=seat.folder/FILE
    text=json.dumps(data,sort_keys=True)+'\n'
    if not path.exists() or path.read_text()!=text:
        durable.replace_file(path,text)


def cached(seat):
    """Automatic human status reads owned metadata and record stat, never conversation bytes."""
    try:
        data=json.loads((seat.folder/FILE).read_text())
        if data.get('format')!=1 or data.get('identity')!=identity(seat):
            return None
        if (seat.folder/'record').read_text().strip()!=data['record']:
            return None
        failure=data['failure']
        if not failure:
            return None
        if data['fingerprint']!=json.loads(json.dumps(record_fingerprint(Path(data['record'])))):
            return TurnFailure('record changed; current error outcome unknown',None,kind='provider error reading unknown')
        at=datetime.fromisoformat(failure['at']) if failure.get('at') else None
        return TurnFailure(words(failure['message']),at,model_output=failure['model_output'],kind=failure['kind'])
    except (OSError,ValueError,KeyError,TypeError):
        return None  # missing/unproved operational metadata establishes no current refusal


def observed(seat):
    if seat.session.human:
        return cached(seat)
    try:
        read=getattr(seat,'record',None)
        record=read() if read else None
        return getattr(seat.harness.state(record),'failure',None) if record is not None else None
    except OSError:
        return None


def obstruction(seat,store=None):
    if 'uncertain' in post.Attempts(seat.ship.runtime).latest(seat.name).values():
        return 'provider error: input/output evidence uncertain; never resent; inspect the owned record'
    failure=observed(seat)
    if failure is None:
        return None
    store=store or post.Store(seat.ship.runtime)
    if not any(m.kind in ('order','question') for m in store.open_for(seat.name)):
        return None
    return f'{failure.kind}: {words(failure.message)}'


def _outcomes(seat,attempts):
    from . import pending,delivery_record
    latest={a.message:a for a in attempts.all() if a.seat==seat.name}
    if not latest:
        return latest,{}
    proofs,errors=pending.journals(seat)
    outcomes={}
    for proof in proofs:
        for attempt in latest.values():
            if attempt.path.name in proof.data['attempts']:
                outcomes[attempt.message]=proof.confirmed(seat)
    for message,attempt in latest.items():
        if message not in outcomes and attempt.result in ('refused','uncertain','carrying'):
            outcomes[message]=delivery_record.receipt_anywhere(seat,post.Store(seat.ship.runtime).get(message),attempt.receipt_since)
    if errors and any(a.result in ('refused','uncertain','carrying') for a in latest.values()):
        raise ValueError('refused-input proof is unreadable')
    return latest,outcomes


def require_clear(seat):
    """An explicit start cannot turn ambiguous or unconfirmed carriage into a replay."""
    from .ship import ShipError
    try:
        latest,outcomes=_outcomes(seat,post.Attempts(seat.ship.runtime))
        for message,attempt in latest.items():
            result=outcomes.get(message)
            if (result=='uncertain' or attempt.result in ('uncertain','carrying') and result not in ('delivered','refused')
                    or attempt.result=='refused' and result!='refused'):
                raise ValueError('provider-error input/output or destination carriage is unproved')
    except (OSError,ValueError,KeyError):
        raise ShipError(f'{seat.name}: provider-error receipt/carriage uncertain; no replay or retirement; inspect the owned record') from None


def unacted(seat,store,attempts):
    latest,outcomes=_outcomes(seat,attempts)
    return [store.get(message) for message,attempt in latest.items()
            if attempt.result not in ('abandoned','watch') and outcomes.get(message)=='refused']
