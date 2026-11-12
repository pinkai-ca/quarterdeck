"""Declared groups: fixed fanout intents and one terminal notice, beside ordinary posts.

The root is an ordinary order to the lead. Copies have their own addressees and
all reply to that root. The journal adds no fields to the immutable Message format.
"""

from __future__ import annotations

from contextlib import ExitStack, nullcontext
import hashlib
import json
from pathlib import Path

from . import durable, post, structure_outcome
from .ship import Ship, ShipError

FAILURE_REASONS = frozenset({'member no longer declared', 'provider refused this copy',
                             'owned pane is dead', 'held at limit with no eligible destination',
                             *structure_outcome.REASONS})
HOLD_REASONS = frozenset({'group evidence is malformed JSON', 'group evidence is inconsistent',
                          'group evidence is unreadable', 'group coordination could not finish'})


def _folder(ship: Ship) -> Path:
    return ship.runtime / 'post' / 'groups'


def _save(path: Path, data: dict) -> None:
    created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created:
        durable.sync_folder(path.parent.parent)
        durable.sync_folder(path.parent.parent.parent)
    durable.replace_file(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _encoded(message: post.Message) -> dict:
    return json.loads(message.to_json())


def _message(data: dict) -> post.Message:
    return post.Message.from_json(json.dumps(data))


def _publish(store: post.Store, message: post.Message) -> None:
    """A cut after publication reuses the exact intent, never another message id."""
    path = store.dir / f'{message.id}.json'
    if path.exists():
        if post.Message.from_json(path.read_text()) != message:
            raise ShipError(f'group post {message.id} differs from its saved intent; retained')
    else:
        store.write(message)


def _load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('format') != 1:
        raise ShipError(f'unknown group journal format in {path}; retained')
    root = _message(data['root'])
    copies = [_message(item) for item in data['copies']]
    if (path.stem != root.id or root.kind != 'order' or root.to != data['lead']
            or not copies or len({m.id for m in copies}) != len(copies)
            or len({m.to for m in copies}) != len(copies)
            or any(m.re != root.id or m.sender != root.to or m.body != root.body
                   or m.kind != 'order' or m.id == root.id for m in copies)):
        raise ShipError(f'inconsistent group intent in {path}; retained')
    if data['notice'] is not None:
        notice = _message(data['notice'])
        if notice.to != root.to or notice.sender != post.SHIP or notice.kind != 'report' or notice.re != root.id:
            raise ShipError(f'inconsistent group notice in {path}; retained')
    failed = data.get('failed', {})
    members = {m.id: m.to for m in copies}
    if (not isinstance(failed, dict) or any(
            identity not in members or not isinstance(outcome, dict)
            or set(outcome) != {'member', 'reason'} or outcome['member'] != members[identity]
            or not isinstance(outcome['reason'], str) or outcome['reason'] not in FAILURE_REASONS
            for identity, outcome in failed.items())):
        raise ShipError(f'inconsistent member outcomes in {path}; retained')
    return data


def submit(ship: Ship, store: post.Store, group_name: str, sender: str, body: str,
           *, re: str | None = None, subject: str | None = None,
           due: str | None = None, speed: str | None = None) -> tuple[post.Message, tuple[post.Message, ...]]:
    """Address a group, or let its lead fan out an existing identical open request.

    Repeating the latter operation with the same root is idempotent, even after a
    crash. A new request without --re is a new order, just as for a single seat.
    """
    group = ship.groups[group_name]
    with post.admission(ship.runtime, '@groups'):
        if re is not None:
            root = store.get(re)
            if (sender != group.lead or root.to != group.lead or root.kind != 'order'
                    or root.body != body):
                raise ShipError('group --re must name an identical order to this lead, forwarded by the lead')
            if any(value is not None and value != getattr(root, key)
                   for key, value in (('subject', subject), ('due', due), ('speed', speed))):
                raise ShipError('group --re cannot change the original order fields')
        else:
            root = store.prepare('order', sender, group.lead, body, subject=subject, due=due, speed=speed)
        path = _folder(ship) / f'{root.id}.json'
        if path.exists():
            data = _load(path)
            if (data['group'] != group_name or _message(data['root']) != root
                    or data['lead'] != group.lead or [m['to'] for m in data['copies']] != list(group.members)):
                raise ShipError('request already has a different group fanout; retained')
        else:
            if re is not None and not store.is_open(root):
                raise ShipError('cannot start a group review of a closed order')
            # Root may not be published yet, so construct copies without Store.prepare's
            # ordinary reference lookup. The journal reserves all identities together.
            copies = []
            taken = {root.short}
            for member in group.members:
                while True:
                    copy = store.prepare('order', group.lead, member, root.body,
                                         subject=root.subject, due=root.due, speed=root.speed)
                    if copy.short not in taken:
                        break
                taken.add(copy.short)
                copies.append(post.Message(copy.id, copy.kind, copy.sender, copy.to, copy.at,
                                           copy.body, root.id, copy.subject, copy.due, copy.speed))
            data = dict(format=1, group=group_name, lead=group.lead,
                        root=_encoded(root), copies=[_encoded(m) for m in copies], notice=None, failed={})
            _save(path, data)  # intent precedes every immutable post
        _fanout(store, data)
        return root, tuple(_message(item) for item in data['copies'])


def _fanout(store: post.Store, data: dict) -> None:
    _publish(store, _message(data['root']))
    for item in data['copies']:
        _publish(store, _message(item))


def advance(ship: Ship, store: post.Store, failures=None, *, outcomes=None) -> list[tuple[str, str]]:
    """Recover partial fanouts and finish groups, from local evidence only.

    ``failures(member, copy)`` returns a fixed, content-free reason or None. No
    deadline or missing report alone proves failure. Positive member outcomes are
    saved for this copy alone; filed reports take priority before the final notice.
    Return newly changed held-group errors, for logging after the whole pass.
    """
    folder = _folder(ship)
    if not folder.exists():
        return []
    changed = []
    with post.admission(ship.runtime, '@groups'):
        for path in sorted(folder.glob('*.json')):
            try:
                _advance_one(path, store, failures, outcomes)
                hold = ship.runtime / 'post/group-holds' / path.name
                if hold.exists():
                    hold.unlink()
                    durable.sync_folder(hold.parent)
            except Exception as error:  # one malformed journal cannot stall healthy groups
                reason = _error_reason(error)
                if _hold(ship.runtime, path, reason):
                    changed.append((path.stem, reason))
    return changed


def _advance_one(path: Path, store: post.Store, failures, outcomes=None) -> None:
    data = _load(path)
    _fanout(store, data)
    copies = [_message(item) for item in data['copies']]
    runtime = store.dir.parent.parent
    if data['notice'] is None:
        saved = dict(data.get('failed', {}))
        for copy in copies:
            if store.closing_report(copy) is not None:
                structure_outcome.clear_wait(runtime, copy.id)
                continue
            if copy.id in saved:
                continue
            reason = failures(copy.to, copy) if failures is not None else None
            observation = outcomes(copy.to, copy) if reason is None and outcomes is not None else nullcontext(None)
            with observation as ended:
                # The observer holds member admission and current lifecycle until
                # this copy's outcome is saved. A concurrent report wins here.
                if store.closing_report(copy) is not None:
                    structure_outcome.clear_wait(runtime, copy.id)
                    continue
                reason = reason or ended
                if reason is not None:
                    if reason not in FAILURE_REASONS:
                        raise ShipError('unknown group member outcome')
                    saved[copy.id] = dict(member=copy.to, reason=reason)
                    data['failed'] = saved
                    _save(path, data)
                    structure_outcome.clear_wait(runtime, copy.id)
        # Freeze the notice against all concurrent closing reports. Take locks in
        # Store.write's order, then publish only after releasing them.
        with ExitStack() as admission:
            for name in sorted({data['lead'], *(m.to for m in copies)}):
                admission.enter_context(post.admission(runtime, name))
            reports = {m.to: report for m in copies if (report := store.closing_report(m)) is not None}
            failed = {m.to: saved[m.id]['reason'] for m in copies if m.to not in reports and m.id in saved}
            if len(reports) + len(failed) != len(copies):
                return
            root = _message(data['root'])
            title = 'all filed' if len(reports) == len(copies) else 'member failed'
            lines = [f'group {data["group"]} · {root.id} · {title}', '', 'Filed reports:']
            lines += [f'- {member}: {report.id}' for member, report in reports.items()] or ['- none']
            if failed:
                lines += ['', 'Failed members:'] + [f'- {member}: {reason}' for member, reason in failed.items()]
            lines += ['', 'Every member has filed or positively failed.',
                      'Combine only the filed reports, retain every dissent, and add no new judgment.',
                      f'Report once to {root.sender} with qd post report --re {root.id}.']
            notice = store.prepare('report', post.SHIP, data['lead'], '\n'.join(lines), re=root.id)
            data['notice'] = _encoded(notice)
            _save(path, data)
    for copy in copies:
        structure_outcome.clear_wait(runtime, copy.id)
    _publish(store, _message(data['notice']))


def _error_reason(error: Exception) -> str:
    if isinstance(error, json.JSONDecodeError):
        return 'group evidence is malformed JSON'
    if isinstance(error, (ShipError, ValueError, KeyError, TypeError)):
        return 'group evidence is inconsistent'
    if isinstance(error, OSError):
        return 'group evidence is unreadable'
    return 'group coordination could not finish'


def _hold(runtime: Path, journal: Path, reason: str) -> bool:
    try:
        signature = hashlib.sha256(journal.read_bytes()).hexdigest()
    except OSError:
        signature = hashlib.sha256(b'unreadable').hexdigest()
    data = dict(format=1, request=journal.stem, reason=reason, signature=signature)
    path = runtime / 'post/group-holds' / journal.name
    try:
        if json.loads(path.read_text()) == data:
            return False
    except (OSError, ValueError):
        pass
    _save(path, data)
    return True


def holds(runtime: Path) -> list[str]:
    """Read-only derived operational holds; never parse review bodies or repair state."""
    folder = runtime / 'post/group-holds'
    try:
        paths = sorted(folder.glob('*.json'))
    except OSError:
        return ['groups held: hold evidence unreadable']
    lines = []
    for path in paths:
        reason = 'hold evidence unreadable'
        try:
            data = json.loads(path.read_text())
            if (data['format'] == 1 and data['request'] == path.stem
                    and data['reason'] in HOLD_REASONS and isinstance(data['signature'], str)):
                reason = data['reason']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        lines.append(f'{post.short_of(path.stem)} group held: {reason}')
    return lines


def member_failure(service, name: str, copy: post.Message) -> str | None:
    """Read existing ownership, quota and failure evidence; never contact a provider.

    Operational reasons contain no provider text or reviewer body. A missing
    launch, unreadable evidence or identity uncertainty remains pending.
    """
    if name not in service.ship.seats:
        return 'member no longer declared'
    from . import refusal
    if copy.id in refusal.excluded(service.runtime, name):
        return 'provider refused this copy'
    return current_seat_failure(service, name)


def current_seat_failure(service, name: str) -> str | None:
    """Positive current dead/held-limit evidence, also usable without an order.

    This bounded common observation never reads a refusal journal. None means
    no proved terminal failure, including missing or transitioning lifecycle
    evidence. No readiness is disarmed and no provider allowance is refreshed.
    """
    from . import watch
    from .tmux import TmuxError

    if name not in service.ship.seats:
        return None
    try:
        seat = service.seat(name)
        # Observe under the existing lifecycle lock without disarming readiness
        # or beginning a transition. A busy/unknown transition remains pending.
        with seat._lifecycle(automatic=True):
            launched = seat.launched()
            if launched is None:
                return None
            pane = seat.pane()
            if pane is None:
                return None  # absence alone is not a positively settled failure
            reason = None
            if pane.dead:
                reason = 'owned pane is dead'
            else:
                held, unknown = watch.limit_holds(service.ship, [seat])
                if name in held and not unknown:
                    reason = 'held at limit with no eligible destination'
            return reason if seat.launched() == launched else None
    except (ShipError, OSError, TmuxError):
        return None
