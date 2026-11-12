"""Declared groups: fixed fanout intents and one terminal notice, beside ordinary posts.

The root is an ordinary order to the lead. Copies have their own addressees and
all reply to that root. The journal adds no fields to the immutable Message format.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import durable, post
from .ship import Ship, ShipError


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
                        root=_encoded(root), copies=[_encoded(m) for m in copies], notice=None)
            _save(path, data)  # intent precedes every immutable post
        _fanout(store, data)
        return root, tuple(_message(item) for item in data['copies'])


def _fanout(store: post.Store, data: dict) -> None:
    _publish(store, _message(data['root']))
    for item in data['copies']:
        _publish(store, _message(item))


def advance(ship: Ship, store: post.Store, failures=None) -> None:
    """Recover partial fanouts and finish groups, from local evidence only.

    ``failures(member, copy)`` returns a fixed, content-free reason or None. No
    deadline or missing report alone proves failure. Filed reports take priority.
    Membership is the saved snapshot, even if the declaration changes later.
    """
    folder = _folder(ship)
    if not folder.exists():
        return
    with post.admission(ship.runtime, '@groups'):
        for path in sorted(folder.glob('*.json')):
            data = _load(path)
            _fanout(store, data)
            if data['notice'] is None:
                copies = [_message(item) for item in data['copies']]
                reports = {m.to: report for m in copies if (report := store.closing_report(m)) is not None}
                failed = {m.to: reason for m in copies if m.to not in reports and failures is not None
                          and (reason := failures(m.to, m))}
                if len(reports) != len(copies) and not failed:
                    continue
                root = _message(data['root'])
                title = 'all filed' if len(reports) == len(copies) else 'member failed'
                lines = [f'group {data["group"]} · {root.id} · {title}', '', 'Filed reports:']
                lines += [f'- {member}: {report.id}' for member, report in reports.items()] or ['- none']
                if failed:
                    lines += ['', 'Failed members:'] + [f'- {member}: {reason}' for member, reason in failed.items()]
                pending = [m.to for m in copies if m.to not in reports and m.to not in failed]
                if pending:
                    lines += ['', 'Not yet filed: ' + ', '.join(pending)]
                lines += ['', 'Combine only the filed reports, retain every dissent, and add no new judgment.',
                          f'Report once to {root.sender} with qd post report --re {root.id}.']
                notice = store.prepare('report', post.SHIP, data['lead'], '\n'.join(lines), re=root.id)
                data['notice'] = _encoded(notice)
                _save(path, data)  # a cut before/after publication reuses this notice
            _publish(store, _message(data['notice']))


def member_failure(service, name: str, copy: post.Message) -> str | None:
    """Read existing ownership, quota and failure evidence; never contact a provider.

    Operational reasons contain no provider text or reviewer body. A missing
    launch, unreadable evidence or identity uncertainty remains pending.
    """
    if name not in service.ship.seats:
        return 'member no longer declared'
    from . import quota, refusal
    if copy.id in refusal.excluded(service.runtime, name):
        return 'provider refused this copy'
    try:
        seat = service.seat(name)
    except ShipError:
        return None  # unresolved selection is not a proved member failure
    launched = seat.launched()
    if launched is None:
        return None
    pane = seat.pane()
    if pane is not None and pane.dead:
        return 'owned pane is dead'
    if pane is None:
        # A matching saved launch with an absent pane is positively down. A
        # reused pane id belongs to someone else, so it supplies no failure fact.
        if service.tmux.pane(launched.pane) is None:
            return 'saved launch pane is absent'
        return None
    if quota.current_walled(seat, service.ledger):
        return 'current pool is at its wall'
    return None
