"""Small hand context keeps its open work; worker text stays byte-for-byte stable."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from quarterdeck import post, ship as shipmod
from quarterdeck.seat import Seat


AT = datetime(2032, 1, 2, 12, tzinfo=timezone.utc)


def world(folder, role):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'ship.toml').write_text(f'''[ship]
name = "Synthetic context"
operator = "issuer"
code = "checkout"
[pools.fake]
provider = "synthetic"
[sessions.crew]
title = "Synthetic crew"
[seats.recipient]
session = "crew"
role = "{role}"
harness = "fake"
pool = "fake"
model = "fake-1"
[seats.issuer]
session = "crew"
role = "worker"
harness = "fake"
pool = "fake"
model = "fake-1"
''')
    ship = shipmod.load(folder)
    store = post.Store(ship.runtime)
    def add(number, kind, sender, to, body, *, re=None, subject=None, due=None):
        message = post.Message(f'{kind[:3]}-20320102-1200-{sender}-{number:06x}', kind, sender, to,
                               AT + timedelta(seconds=number), body, re=re, subject=subject, due=due)
        store._write(message)
        return message
    old = add(1, 'order', 'issuer', 'recipient', 'Closed synthetic task.')
    closing = add(2, 'report', 'recipient', 'issuer', 'Synthetic task completed.', re=old.id)
    opened = add(3, 'order', 'issuer', 'recipient', 'Read the synthetic packet.',
                 subject='Current task', due='literal due')
    history = add(4, 'report', 'issuer', 'recipient', 'Unrelated synthetic received history.')
    sent = add(5, 'question', 'recipient', 'issuer', 'An unrelated outgoing wait.')
    question = add(6, 'question', 'recipient', 'issuer', 'Which synthetic option?', re=opened.id)
    answer = add(7, 'answer', 'issuer', 'recipient', 'Use the second synthetic option.', re=question.id)
    incoming = add(8, 'question', 'issuer', 'recipient', 'An open incoming question.')
    seat = Seat(ship, ship.seat('recipient'), object())
    return SimpleNamespace(ship=ship, store=store, seat=seat, old=old, closing=closing,
                           opened=opened, history=history, sent=sent, question=question,
                           answer=answer, incoming=incoming)
