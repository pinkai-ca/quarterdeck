"""Order threads and timestamped evidence, never a guess from prose or quoted ids."""

from dataclasses import dataclass
from datetime import datetime
import hashlib

from .post_message import delivered_forms


def moment(value):
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else value
        return at if isinstance(at, datetime) and at.tzinfo is not None else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Verdict:
    status: str
    orders: tuple[str, ...]
    reason: str
    evidence: tuple[dict, ...] = ()

    @property
    def key(self):
        return self.orders[0] if self.status == 'attributed' else {
            'shared': '[shared]', 'unresolved': '[unresolved]', 'unassociated': '-',
        }[self.status]


class Threads:
    def __init__(self, store):
        self.messages = store.all()
        self.by_id, self.conflicted = {}, set()
        for message in self.messages:
            if message.id in self.by_id and message != self.by_id[message.id]:
                self.conflicted.add(message.id)
            self.by_id[message.id] = message
        self.closed = {}
        for message in self.messages:
            order = self.by_id.get(message.re)
            if (message.kind == 'report' and order and order.kind == 'order'
                    and message.sender == order.to and message.at >= order.at):
                self.closed.setdefault(order.id, message)
        self.envelopes, self.message_headers = {}, {}
        for message in self.messages:
            forms = tuple(form.strip() for form in delivered_forms(message))
            self.message_headers[id(message)] = tuple(form.partition('\n')[0] for form in forms)
            for form in forms:
                self.envelopes[form] = message
        self.by_header = {}
        for envelope, message in self.envelopes.items():
            self.by_header.setdefault(envelope.partition('\n')[0], []).append((envelope, message))

    def scope(self, seat, ended):
        """Fingerprint only posts that can affect this seat's historical verdicts.

        Reply ancestors and closing reports affect claim validity and thread roots.
        Identity variants and envelope-header candidates preserve ambiguity too.
        The index and message digests live for this graph alone.
        """
        ended = moment(ended)
        if ended is None:
            return hashlib.sha256(b'').hexdigest()
        if not hasattr(self, '_scope_index'):
            by_seat, variants = {}, {}
            positions = {id(message): i for i, message in enumerate(self.messages)}
            digests = []
            for i, message in enumerate(self.messages):
                for name in {message.sender, message.to}:
                    by_seat.setdefault(name, []).append(i)
                variants.setdefault(message.id, []).append(i)
                digests.append(hashlib.sha256(message.to_json().encode()).digest())
            self._scope_index = by_seat, variants, positions, digests
        by_seat, variants, positions, digests = self._scope_index
        selected = {i for i in by_seat.get(seat, ()) if self.messages[i].at <= ended}
        for i in tuple(selected):
            message = self.messages[i]
            if message.to == seat:
                for header in self.message_headers[id(message)]:
                    selected.update(positions[id(m)] for _, m in self.by_header.get(header, ()))
        pending, visited = list(selected), set()
        while pending:
            i = pending.pop()
            if i in visited:
                continue
            visited.add(i)
            message = self.messages[i]
            following = [*variants.get(message.id, ()), *variants.get(message.re, ())]
            closed = self.closed.get(message.id) if message.kind == 'order' else None
            if closed is not None and closed.at <= ended:
                following.append(positions[id(closed)])
            pending.extend(j for j in following if j not in visited)
        digest = hashlib.sha256()
        for i in sorted(visited):
            digest.update(digests[i])
        return digest.hexdigest()

    def valid(self, message):
        target = self.by_id.get(message.re)
        if message.kind == 'ack':
            return bool(target and target.kind == 'order' and target.to == message.sender
                        and target.at <= message.at and self.open_at(target, message.at))
        if message.kind == 'report' and target and target.kind == 'order':
            return self.closed.get(target.id) == message
        return True

    def open_at(self, order, at):
        closed = self.closed.get(order.id)
        return order.at <= at and (closed is None or closed.at > at)

    def root(self, message):
        """Only explicit re edges join orders; an unlinked order is its own change."""
        seen, order = set(), None
        while message is not None:
            if message.id in self.conflicted:
                return None, 'conflicting immutable message identity'
            if message.id in seen:
                return None, 'cyclic reply thread'
            seen.add(message.id)
            if not self.valid(message):
                return None, 'invalid claim or non-closing report in reply thread'
            if message.kind == 'order':
                order = message.id
            if not message.re:
                return order, None
            parent = self.by_id.get(message.re)
            if parent is None:
                return None, 'missing reply target'
            if parent.at > message.at:
                return None, 'reply precedes its target'
            message = parent
        return order, None

    def received(self, text, at, seat):
        """Exact complete top-level post envelopes only; quoted ids/body fragments prove nothing."""
        at = moment(at)
        from .input_receipt import without_brief_head
        text = without_brief_head(text).strip()  # a standing-orders head before the envelopes is no envelope
        found = []
        while text:
            matches = [(envelope, m) for envelope, m in self.by_header.get(text.partition('\n')[0], ())
                       if text == envelope or text.startswith(envelope + '\n')]
            # A legacy non-obligation adds its inbox footer to the current complete
            # envelope. Consume that longer form when both identify this same immutable
            # message; competing message identities retain the ambiguity refusal.
            if matches and all(message == matches[0][1] for _, message in matches):
                matches = [max(matches, key=lambda item: len(item[0]))]
            if len(matches) != 1:
                return []  # no partial/truncated bundle or arbitrary surrounding stdout
            envelope, message = matches[0]
            if at is None or message.at > at or message.to != seat:
                return []
            found.append({'message': message.id, 'at': at.isoformat()})
            text = text[len(envelope):].strip()
        return found

    def decide(self, seat, started, ended, received=(), source_error=None):
        start, end = moment(started), moment(ended)
        if start is None or end is None or end < start:
            return Verdict('unresolved', (), 'turn interval unavailable or invalid')
        evidence, errors = [], []
        def add(message, route, at, retained=False):
            if not self.valid(message):
                return
            order, error = self.root(message)
            evidence.append({'message': message.id, 'route': route, 'at': at.isoformat(), 'order': order, 'retained': retained})
            if error:
                errors.append(error)
        for message in self.messages:
            if message.sender == seat and start <= message.at <= end:
                add(message, 'posted', message.at)
        for entry in received:
            at = moment(entry.get('at'))
            message = self.by_id.get(entry.get('message'))
            if at is not None and start <= at <= end:
                if message is None:
                    errors.append('received post missing from store')
                elif message.to == seat and message.at <= at:
                    add(message, entry.get('route', 'input'), at, entry.get('retained', False))
        direct = {e['order'] for e in evidence if e['order']}
        if not direct and not errors:
            # A prior claim is a fallback, not permission to discard conflicting direct work.
            claims = [m for m in self.messages if m.kind == 'ack' and m.sender == seat
                      and m.at < start and self.valid(m) and self.open_at(self.by_id[m.re], start)]
            if claims:
                latest = max(m.at for m in claims)
                for claim in claims:
                    if claim.at == latest:
                        add(claim, 'claim at turn start', claim.at)
            else:
                orders = [m for m in self.messages if m.kind == 'order' and m.to == seat and m.at <= end
                          and (m.id not in self.closed or self.closed[m.id].at >= start)]
                for order in orders:
                    add(order, 'open interval', max(start, order.at))
        orders = tuple(sorted({e['order'] for e in evidence if e['order']}))
        if errors:
            return Verdict('unresolved', orders, '; '.join(sorted(set(errors))), tuple(evidence))
        if len(orders) > 1 and not direct:
            return Verdict('unresolved', orders, 'competing claims or open orders; no unique work evidence', tuple(evidence))
        if len(orders) > 1:
            return Verdict('shared', orders, 'several distinct order threads; measurements counted once', tuple(evidence))
        if orders:
            basis = 'timestamped post/thread evidence' if direct else ('valid claim at turn start' if any(e['route'] == 'claim at turn start' for e in evidence) else 'sole open-order interval fallback')
            return Verdict('attributed', orders, basis + ('; retained verified incoming evidence' if any(e['retained'] for e in evidence) else '') + (f'; {source_error}' if source_error else ''), tuple(evidence))
        return Verdict('unresolved' if source_error else 'unassociated', (),
                       source_error or 'no order association in available evidence', tuple(evidence))
