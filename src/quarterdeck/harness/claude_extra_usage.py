"""Claude extra-usage observations for display and private notices only."""

from datetime import timedelta, timezone
import json
import sqlite3

from .. import durable, post
from ..ship import ShipError
from ..usage_cache import MAX_AGE, parse
from . import native_name
from .claude_limits import reported_literal


SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_extra_usage (
    pool TEXT NOT NULL, read_at TEXT NOT NULL, setting TEXT NOT NULL, detail TEXT,
    literals TEXT,
    PRIMARY KEY (pool, read_at)
);
"""
SETTING_DETAILS = {
    'off': {'org_level_disabled'},
    'unverified': {None},
    'unavailable': {'setting missing', 'setting malformed', 'conflicting evidence'},
}


def _pair(text):
    if not isinstance(text, str) or len(text) > 160:
        return None
    try:
        pair = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if (isinstance(pair, list) and len(pair) == 2 and any(value is not None for value in pair)
            and all(reported_literal(value) for value in pair)):
        return pair
    return None


def remember(db, pool, observation):
    """Keep only normalized scalars and the proved paying pool in the record transaction."""
    at = observation.at
    if (at.tzinfo is None or observation.setting not in SETTING_DETAILS
            or observation.detail not in SETTING_DETAILS[observation.setting]):
        return
    literals = json.dumps(observation.literals, separators=(',', ':')) if observation.literals is not None else None
    if (observation.setting == 'unverified' and _pair(literals) is None
            or observation.setting != 'unverified' and literals is not None):
        return
    db.execute('''INSERT INTO claude_extra_usage VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(pool,read_at) DO UPDATE SET setting='unavailable', detail='conflicting evidence', literals=NULL
        WHERE (setting IS NOT excluded.setting OR detail IS NOT excluded.detail OR literals IS NOT excluded.literals)
          AND detail IS NOT 'conflicting evidence' ''',
        (pool, at.astimezone(timezone.utc).isoformat(), observation.setting, observation.detail, literals))


def pools(ship):
    return sorted(pool.name for pool in getattr(ship, 'pools', {}).values()
                  if pool.kind == 'subscription' and native_name(pool.provider) == 'claude')


def _text(pair):
    status, disabled_reason = (json.dumps(value) if value is not None else 'unknown' for value in pair)
    return f'overageStatus={status}, overageDisabledReason={disabled_reason}'


def collect(db, names, now):
    table = db is not None and db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='claude_extra_usage'").fetchone()
    for pool in names:
        row = (db.execute('SELECT read_at,setting,detail,literals FROM claude_extra_usage WHERE pool=? '
                          'ORDER BY read_at DESC LIMIT 1', (pool,)).fetchone() if table else None)
        at, setting, detail, literals = row or (None, 'unavailable', 'no provider reading', None)
        observed = parse(at)
        pair = _pair(literals)
        if row is not None and (observed is None or observed > now or setting not in SETTING_DETAILS
                                or detail not in SETTING_DETAILS[setting]
                                or (setting == 'unverified') != (literals is not None)
                                or (literals is not None and pair is None)):
            setting, detail, pair = 'unavailable', 'stored reading malformed', None
        if setting == 'off':
            pair = ('rejected', 'org_level_disabled')
        elif pair is not None:
            detail = _text(pair)
        if observed is not None and now - observed >= timedelta(seconds=MAX_AGE):
            prior = setting + (f' ({detail})' if detail else '')
            setting, detail = 'stale', f'last reported {prior}'
        yield dict(pool=pool, setting=setting, reason=detail, read_at=observed.isoformat() if observed else None,
                   overageStatus=pair[0] if pair else None, overageDisabledReason=pair[1] if pair else None,
                   cumulative_spend=None, monthly_limit=None, currency=None, period=None)


def readings(ship):
    from ..ledger import Ledger
    names, now = pools(ship), post.now()
    if names:
        try:
            book = Ledger.reading(ship.runtime / 'ledger.sqlite')
            if book is not None:
                with book:
                    return list(collect(book.db, names, now))
        except (OSError, sqlite3.Error):
            return [dict(row, reason='ledger unreadable') for row in collect(None, names, now)]
    return list(collect(None, names, now))


def describe(row):
    setting = row['setting'] + (f" ({row['reason']})" if row['reason'] else '')
    at = row['read_at'] or 'unknown'
    return (f'extra usage: {setting} · read {at} · '
            'spend, monthly limit, currency and billing period unknown')


def _body(pool, at, pair):
    return f'{pool}: Claude extra usage is unverified. At {at} it reported {_text(pair)}.'


def notify(service):
    """Publish one immutable Desk notice for a new unverified reading or changed pair."""
    from ..ledger import Ledger

    names = pools(service.ship)
    recipient = next((seat.name for seat in service.ship.seats.values() if seat.role == 'desk'), None)
    if not names or recipient is None:
        return
    path = service.runtime / 'claude-extra-usage-alerts.json'
    try:
        saved = json.loads(path.read_text())
    except FileNotFoundError:
        saved = {}
    except (OSError, ValueError):
        raise ShipError('Claude extra-usage notice journal unreadable') from None
    if not isinstance(saved, dict):
        raise ShipError('Claude extra-usage notice journal malformed')

    def save():
        durable.replace_file(path, json.dumps(saved, sort_keys=True) + '\n')

    for pool in names:
        if pool in saved:
            _publish(service, pool, recipient, saved[pool], save)
    now = post.now().astimezone(timezone.utc)
    oldest = (now - timedelta(seconds=MAX_AGE)).isoformat()
    try:
        book = Ledger.reading(service.runtime / 'ledger.sqlite')
        if book is None:
            return
        with book:
            table = book.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='claude_extra_usage'").fetchone()
            if not table:
                return
            readings = {pool: book.db.execute(
                "SELECT read_at,setting,detail,literals FROM claude_extra_usage WHERE pool=? AND read_at>? "
                "AND read_at<=? AND setting IN ('unverified','off') ORDER BY read_at",
                (pool, max(oldest, saved.get(pool, {}).get('at', oldest)), now.isoformat())).fetchall() for pool in names}
    except (OSError, sqlite3.Error):
        return

    changed = False
    for pool, rows in readings.items():
        for at, setting, detail, key in rows:
            if (parse(at) is None or detail not in SETTING_DETAILS[setting]
                    or (setting == 'unverified' and (pair := _pair(key)) is None)
                    or (setting == 'off' and key is not None)):
                continue
            previous_key = saved.get(pool, {}).get('key')
            saved[pool] = dict(at=at, key=key, intent=None, published=True)
            changed = True
            if setting == 'unverified' and previous_key != key:
                message = service.store.prepare('report', post.SHIP, recipient, _body(pool, at, pair))
                saved[pool].update(intent=message.to_json(), published=False)
                save()
                _publish(service, pool, recipient, saved[pool], save)
                changed = False  # the pending intent and its publication saved the whole journal
    if changed:
        save()


def _publish(service, pool, recipient, entry, save):
    from ..compaction_alert import publish

    if (not isinstance(entry, dict) or type(entry.get('published')) is not bool or parse(entry.get('at')) is None
            or entry.get('key') is not None and _pair(entry['key']) is None):
        raise ShipError('Claude extra-usage notice journal malformed')
    if entry['published']:
        return
    try:
        message = post.Message.from_json(entry['intent'])
    except (ValueError, TypeError, KeyError):
        raise ShipError('Claude extra-usage notice intent unreadable') from None
    pair = _pair(entry.get('key'))
    if (pair is None or (message.kind, message.sender, message.to, message.re) != ('report', post.SHIP, recipient, None)
            or message.body != _body(pool, entry['at'], pair)):
        raise ShipError('Claude extra-usage notice intent malformed')
    with post.admission(service.runtime, recipient), post.fixed_report(service.store, message):
        publish(service.runtime, entry['intent'])
    entry['published'] = True
    save()
