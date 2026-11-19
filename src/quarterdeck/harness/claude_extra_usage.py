"""Claude extra-usage observations for display and private notices only."""

from datetime import timedelta, timezone
import hashlib
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
CREATE TABLE IF NOT EXISTS claude_extra_usage_changes (
    sequence INTEGER PRIMARY KEY, pool TEXT NOT NULL, read_at TEXT NOT NULL,
    setting TEXT NOT NULL, detail TEXT, literals TEXT, inherited INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS claude_extra_usage_instance (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1), identity TEXT NOT NULL
);
INSERT OR IGNORE INTO claude_extra_usage_instance VALUES (1,lower(hex(randomblob(16))));
CREATE INDEX IF NOT EXISTS claude_extra_usage_changes_pool ON claude_extra_usage_changes(pool, sequence);
INSERT INTO claude_extra_usage_changes(pool,read_at,setting,detail,literals,inherited)
    SELECT pool,read_at,setting,detail,literals,1 FROM claude_extra_usage
    WHERE detail IS NOT 'setting missing' AND NOT EXISTS (SELECT 1 FROM claude_extra_usage_changes)
    ORDER BY read_at,pool;
CREATE TRIGGER IF NOT EXISTS claude_extra_usage_change_insert AFTER INSERT ON claude_extra_usage
WHEN new.detail IS NOT 'setting missing'
BEGIN
    INSERT INTO claude_extra_usage_changes(pool,read_at,setting,detail,literals,inherited)
    VALUES (new.pool,new.read_at,new.setting,new.detail,new.literals,0);
END;
CREATE TRIGGER IF NOT EXISTS claude_extra_usage_change_update AFTER UPDATE ON claude_extra_usage
WHEN new.detail IS NOT 'setting missing'
BEGIN
    INSERT INTO claude_extra_usage_changes(pool,read_at,setting,detail,literals,inherited)
    VALUES (new.pool,new.read_at,new.setting,new.detail,new.literals,0);
END;
"""
SETTING_DETAILS = {
    'off': {'org_level_disabled'},
    'unverified': {None},
    'unavailable': {'setting missing', 'setting malformed', 'conflicting evidence'},
}
OFF = 'off'
MALFORMED = 'setting malformed'
CONFLICT = 'conflicting evidence'
UPGRADE_STATES = {MALFORMED: 'malformed', CONFLICT: 'conflicting'}
NOTICE_BATCH = 64  # per pool per pass, with no age cutoff


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
        ON CONFLICT(pool,read_at) DO UPDATE SET
            setting=CASE WHEN detail='setting missing' THEN excluded.setting ELSE 'unavailable' END,
            detail=CASE WHEN detail='setting missing' THEN excluded.detail ELSE 'conflicting evidence' END,
            literals=CASE WHEN detail='setting missing' THEN excluded.literals ELSE NULL END
        WHERE (setting IS NOT excluded.setting OR detail IS NOT excluded.detail OR literals IS NOT excluded.literals)
          AND detail IS NOT 'conflicting evidence' AND excluded.detail IS NOT 'setting missing' ''',
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


def _unverified_body(pool, at, pair):
    return f'{pool}: Claude extra usage is unverified. At {at} it reported {_text(pair)}.'


def _notice_body(pool, at, key, age):
    if key == MALFORMED:
        body = f'{pool}: Claude reported malformed extra-usage fields at {at}.'
    elif key == CONFLICT:
        body = f'{pool}: Claude extra-usage readings conflict at {at}.'
    else:
        body = _unverified_body(pool, at, _pair(key))
    return f'{body} Reading age when queued: {age} seconds.'


def _key(setting, detail, raw):
    if setting == OFF and detail == 'org_level_disabled' and raw is None:
        return OFF
    if setting == 'unavailable' and detail in (MALFORMED, CONFLICT) and raw is None:
        return detail
    if setting == 'unverified' and detail is None and (pair := _pair(raw)) is not None:
        if pair == ['rejected', 'org_level_disabled']:
            return OFF
        return json.dumps(pair, separators=(',', ':'))
    return None


def _hex(value, length):
    return isinstance(value, str) and len(value) == length and all(c in '0123456789abcdef' for c in value)


def _receipt(at, key):
    fields = (at.astimezone(timezone.utc).isoformat(), key)
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def _seen(entry):
    saved = entry.setdefault('seen', [])
    if not isinstance(saved, list) or not all(_hex(value, 64) for value in saved):
        raise ShipError('Claude extra-usage notice receipts malformed')
    return set(saved)


def _upgrade_evidence(db, pool, boundary, now):
    states = {}
    receipts = []
    oldest = newest = None
    for at, setting, detail, literals in db.execute(
            'SELECT read_at,setting,detail,literals FROM claude_extra_usage_changes '
            'WHERE pool=? AND inherited=1 AND read_at<? ORDER BY read_at', (pool, boundary)):
        key = _key(setting, detail, literals)
        observed = parse(at)
        if key is None or observed is None:
            continue
        receipts.append(_receipt(observed, key))
        if key == OFF:
            continue
        label = UPGRADE_STATES.get(key, 'unverified')
        states[label] = states.get(label, 0) + 1
        oldest = oldest or at
        newest = at
    if not states:
        return None, receipts
    latest = None
    for at, setting, detail, literals in db.execute(
            'SELECT read_at,setting,detail,literals FROM claude_extra_usage '
            "WHERE pool=? AND read_at<=? AND detail IS NOT 'setting missing' ORDER BY read_at DESC",
            (pool, now.isoformat())):
        if parse(at) is not None and (key := _key(setting, detail, literals)) is not None:
            latest = [at, key]
            break
    return dict(states=states, oldest=oldest, newest=newest, latest=latest), receipts


def _upgrade_body(pool, evidence):
    if not isinstance(evidence, dict):
        raise ShipError('Claude extra-usage upgrade evidence malformed')
    states, latest = evidence.get('states'), evidence.get('latest')
    oldest, newest = parse(evidence.get('oldest')), parse(evidence.get('newest'))
    if (not isinstance(states, dict) or not states or oldest is None or newest is None or oldest > newest
            or any(label not in ('unverified', *UPGRADE_STATES.values()) or type(count) is not int or count < 1
                   for label, count in states.items())
            or latest is not None and (not isinstance(latest, list) or len(latest) != 2 or parse(latest[0]) is None
                                       or latest[1] is None or not _known_key(latest[1]))):
        raise ShipError('Claude extra-usage upgrade evidence malformed')
    counts = ', '.join(f'{count} {label}' for label, count in sorted(states.items()))
    latest_words = 'unavailable'
    if latest is not None:
        at, key = latest
        if key == OFF:
            reading = 'off (org_level_disabled)'
        elif key in UPGRADE_STATES:
            reading = UPGRADE_STATES[key]
        else:
            reading = f'unverified ({_text(_pair(key))})'
        latest_words = f'{reading}, observed at {at}'
    return (f'{pool}: Claude extra-usage upgrade summary. Before the prior notice checkpoint, '
            f'{sum(states.values())} retained readings were outside the verified off pair ({counts}). '
            f'Oldest: {evidence["oldest"]}. Newest: {evidence["newest"]}. '
            f'Latest retained reading: {latest_words}. Earlier notices for those historical readings '
            'cannot be proved either way. Spend is unknown.')


def _work(db, pool, entry, instance, now):
    columns = 'sequence,read_at,setting,detail,literals,inherited'
    if entry.get('instance') != instance:
        entry = {}  # sequences and deferred rows belong to the ledger that assigned them
    # A clock-ahead row must wait without holding up later arrivals for this pool.
    due = []
    for sequence, at in entry.get('deferred', ()):
        if parse(at) <= now:
            row = db.execute(f'SELECT {columns} FROM claude_extra_usage_changes WHERE pool=? AND sequence=?',
                             (pool, sequence)).fetchone()
            if row is not None:
                due.append(row)
                if len(due) == NOTICE_BATCH:
                    return due
    new = db.execute(f'SELECT {columns} FROM claude_extra_usage_changes WHERE pool=? AND sequence>? '
                     'ORDER BY sequence LIMIT ?', (pool, entry.get('sequence', 0), NOTICE_BATCH - len(due)))
    return [*due, *new]


def _checkpoint(entry):
    key = entry['key'] or (OFF if entry['at'] is not None else None)
    if key is not None and (at := parse(entry['at'])) is not None:
        return key, _receipt(at, key)
    return key, None


def notify(service):
    """Publish changed readings in ledger arrival order, retaining one immutable intent per pool."""
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
            entry = saved[pool]
            _publish(service, pool, recipient, entry, save)
            if 'upgrade' in entry:
                _publish_upgrade(service, pool, recipient, entry['upgrade'], save)
    now = post.now().astimezone(timezone.utc)
    try:
        book = Ledger.reading(service.runtime / 'ledger.sqlite')
        if book is None:
            return
        with book:
            book.db.execute('BEGIN')
            identity = book.db.execute('SELECT identity FROM claude_extra_usage_instance WHERE singleton=1').fetchone()
            if identity is None or not _hex(identity[0], 32):
                raise ShipError('Claude extra-usage ledger instance unreadable')
            instance = identity[0]
            upgrades = {}
            for pool in names:
                entry = saved.get(pool, {})
                boundary = entry.get('legacy_at', entry.get('at'))
                if boundary is not None and 'upgrade' not in entry:
                    upgrades[pool] = _upgrade_evidence(book.db, pool, boundary, now)
            work = {pool: _work(book.db, pool, saved.get(pool, {}), instance, now) for pool in names}
    except (OSError, sqlite3.Error):
        return

    for pool, (evidence, receipts) in upgrades.items():
        intent = (service.store.prepare('report', post.SHIP, recipient, _upgrade_body(pool, evidence)).to_json()
                  if evidence is not None else None)
        entry = saved[pool]
        seen = _seen(entry)
        if 'sequence' not in entry and (checkpoint := _checkpoint(entry)[1]) is not None:
            receipts.append(checkpoint)
        entry['seen'].extend(dict.fromkeys(receipt for receipt in receipts if receipt not in seen))
        upgrade = entry['upgrade'] = dict(evidence=evidence, intent=intent, published=intent is None)
        save()
        _publish_upgrade(service, pool, recipient, upgrade, save)

    changed = False
    for pool, rows in work.items():
        if not rows:
            continue
        entry = saved.setdefault(pool, dict(at=None, key=None, intent=None, published=True))
        seen = _seen(entry)
        if 'sequence' not in entry:
            # The old journal proves its boundary and current key, but has no older delivery history.
            key, checkpoint = _checkpoint(entry)
            entry.update(legacy_at=entry['at'], current_key=key)
            if checkpoint is not None and checkpoint not in seen:
                entry['seen'].append(checkpoint)
                seen.add(checkpoint)
        if entry.get('instance') != instance:
            entry.update(instance=instance, sequence=0, deferred=[])
        legacy_at = parse(entry.get('legacy_at'))
        for sequence, at, setting, detail, literals, inherited in rows:
            entry['sequence'] = max(entry['sequence'], sequence)
            changed = True
            observed = parse(at)
            if observed is None:
                continue
            if observed > now:
                entry['deferred'].append([sequence, at])
                continue
            if [sequence, at] in entry['deferred']:
                entry['deferred'].remove([sequence, at])
            key = _key(setting, detail, literals)
            if key is None:
                continue
            receipt = _receipt(observed, key)
            if receipt in seen or (key != CONFLICT and _receipt(observed, CONFLICT) in seen):
                continue  # retained conflicts stay conflicts if their individual readings are reconstructed
            entry['seen'].append(receipt)
            seen.add(receipt)
            if inherited and legacy_at is not None and observed < legacy_at:
                continue  # the settled upgrade summary stands for this history
            if key == entry['current_key']:
                continue
            entry['current_key'] = key
            if key == OFF:
                continue
            age = int((now - observed).total_seconds())
            message = service.store.prepare('report', post.SHIP, recipient, _notice_body(pool, at, key, age))
            entry.update(at=at, key=key, intent=message.to_json(), published=False, age=age)
            save()
            _publish(service, pool, recipient, entry, save)
            changed = False  # the pending intent and its publication saved the whole journal
    if changed:
        save()


def _known_key(key):
    return key in (None, OFF, MALFORMED, CONFLICT) or _pair(key) is not None


def _publish(service, pool, recipient, entry, save):
    if not isinstance(entry, dict) or type(entry.get('published')) is not bool:
        raise ShipError('Claude extra-usage notice journal malformed')
    at, key = entry.get('at'), entry.get('key')
    if ('at' not in entry or 'key' not in entry or at is not None and parse(at) is None
            or not _known_key(key)):
        raise ShipError('Claude extra-usage notice journal malformed')
    modern = 'sequence' in entry
    if modern:
        sequence, legacy_at, deferred = entry['sequence'], entry.get('legacy_at'), entry.get('deferred')
        if (type(sequence) is not int or sequence < 0 or not _hex(entry.get('instance'), 32)
                or legacy_at is not None and parse(legacy_at) is None
                or 'current_key' not in entry or not _known_key(entry['current_key'])
                or not isinstance(deferred, list) or not all(
                    isinstance(item, list) and len(item) == 2 and type(item[0]) is int
                    and 0 < item[0] <= sequence and parse(item[1]) is not None for item in deferred)):
            raise ShipError('Claude extra-usage notice journal malformed')
    if entry['published']:
        return
    expected = None
    if modern:
        age = entry.get('age')
        if at is not None and key not in (OFF, None) and type(age) is int and age >= 0:
            expected = _notice_body(pool, at, key, age)
    elif at is not None and (pair := _pair(key)) is not None:
        expected = _unverified_body(pool, at, pair)
    _publish_intent(service, recipient, entry, expected, save)


def _publish_upgrade(service, pool, recipient, upgrade, save):
    if not isinstance(upgrade, dict) or type(upgrade.get('published')) is not bool:
        raise ShipError('Claude extra-usage upgrade journal malformed')
    if not upgrade['published']:
        evidence = upgrade.get('evidence')
        _publish_intent(service, recipient, upgrade, _upgrade_body(pool, evidence), save)


def _publish_intent(service, recipient, entry, expected, save):
    from ..compaction_alert import publish

    try:
        message = post.Message.from_json(entry['intent'])
    except (ValueError, TypeError, KeyError):
        raise ShipError('Claude extra-usage notice intent unreadable') from None
    if (expected is None or (message.kind, message.sender, message.to, message.re) != ('report', post.SHIP, recipient, None)
            or message.body != expected):
        raise ShipError('Claude extra-usage notice intent malformed')
    with post.admission(service.runtime, recipient), post.fixed_report(service.store, message):
        publish(service.runtime, entry['intent'])
    entry['published'] = True
    save()
