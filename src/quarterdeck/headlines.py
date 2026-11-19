"""One display-column budget for the operational status line."""

from typing import NamedTuple
from unicodedata import east_asian_width

WIDTH = 160
SEPARATOR = " · "
POINTER = "qd status"


class Group(NamedTuple):
    details: tuple[str, ...]
    summary: str
    count: int
    short_details: tuple[str, ...] = ()


class Headline(NamedTuple):
    lead: str
    immediate: tuple[Group, ...]
    lower: tuple[Group, ...]


def columns(text: str) -> int:
    """Budget conservatively when presentation marks can widen the preceding glyph."""
    return sum(2 if east_asian_width(char) in ('W', 'F') else 1 for char in text)


def _plain(text):
    return ' '.join(text.split())


def _named(items, label, format_item=str, *, singular=None):
    items = tuple(_plain(format_item(item)) for item in items)
    if len(items) == 1 and singular:
        label = singular
    return Group(items, f'{len(items)} {label}', len(items))


def _count(count, label, detail=None):
    summary = f'{count} {label}'
    return Group((detail or summary,), summary, count)


def note(text, summary):
    return Group((_plain(text),), summary, 1)


def _human_summary(count):
    if count == 1:
        return ('1 needs a human',)
    return (f'{count} need human',) if count else ()


def _human_holds(facts):
    reasons = {name: [] for name in facts.needs_human}
    other_holds = []
    for item in facts.seat_holds:
        name, separator, reason = item.partition(': ')
        if separator and name in reasons:
            reasons[name].append(reason)
        else:
            other_holds.append(item)
    named = _named(facts.needs_human, 'need human', lambda name: f'{name} needs a human', singular='needs a human')
    if not any(reasons.values()):
        return named, other_holds
    short = named.details
    details = tuple(_plain(f"{name}: {'; '.join(reasons[name])}") if reasons[name] else short_item
                    for name, short_item in zip(facts.needs_human, short))
    return named._replace(details=details, short_details=short), other_holds


def sections(facts, *, detailed=False) -> Headline:
    """Presentation only. Categories keep their full items and a shorter count."""
    past, near = [], []
    for item in facts.record_bounds:
        (past if 'record past the read bound' in item else near).append(item)
    barriers = [line for line in facts.server_recovery if line.startswith('service stop barrier')]
    server = [line for line in facts.server_recovery if not line.startswith('service stop barrier')]
    write_stops = [line for line in facts.write_bounds if 'stopped at its write bound' in line]
    write_other = [line for line in facts.write_bounds if 'stopped at its write bound' not in line]
    compacting = [detail for detail in facts.compactions
                  if not any(fallback.startswith(detail + ';') for fallback in facts.manual_fresh)]
    lead = 'SERVICE DOWN' if facts.service == 'down' else f'service {facts.service}'
    if detailed:
        humans = _named(facts.needs_human, 'need human', lambda name: f'{name} needs a human', singular='needs a human')
        seat_holds = facts.seat_holds
    else:
        humans, seat_holds = _human_holds(facts)
    immediate = (
        humans,
        _named(seat_holds, 'seats held', singular='seat held'),
        _named(facts.sign_in_failures, 'sign-in failures', singular='sign-in failure'),
        _named(facts.login, 'login needed', lambda row: f'{row[0]} login needed by {row[1]}'),
        _named(facts.disks, 'disk alerts', lambda row: f'disk: {row[0]} {row[1]}%', singular='disk alert'),
        _named(facts.held, 'held at limit', lambda name: f'{name} held at limit', singular='limit hold'),
        _named(past, 'past read bound'),
        _count(facts.waiting, 'waiting', f'{facts.waiting} waiting, oldest {facts.oldest}m' if facts.waiting else None),
        _count(facts.queued, 'queued', f'{facts.queued} queued behind a turn, oldest {facts.oldest_queued}m'),
        _count(facts.blind, 'blind'),
        _count(facts.unsettled, 'unsettled', f'{facts.unsettled} unsettled after an error'),
        _named(facts.unarmed, 'unarmed', lambda name: f'{name}: no watch armed'),
        _named(facts.pending, 'pending'),
        _named(facts.post_held, 'post holds', singular='post hold'),
        _named(facts.post_waits, 'post waits', singular='post wait'),
    )
    forecasts = facts.pool_forecasts if detailed else facts.pool_forecast_headlines
    lower = (
        _named(facts.pane_obstructions, 'pane holds', singular='pane hold'),
        _named(facts.ending, 'programs ending', singular='program ending'),
        _named(facts.manual_fresh, 'need fresh', singular='needs fresh'),
        _named(write_stops, 'at write bound'),
        _named(write_other, 'write-bound alerts', singular='write-bound alert'),
        _named(facts.leftover_seats, 'seats left over', singular='seat left over'),
        _named(facts.relaunches, 'relaunch holds', singular='relaunch hold'),
        _named(facts.output_limits, 'at output limit'),
        _named((facts.startup_delivery,) if facts.startup_delivery else (), 'startup holds', singular='startup hold'),
        _named(facts.ledger_recovery, 'ledger recovery alerts', singular='ledger recovery alert'),
        _named((facts.accounting,) if facts.accounting else (), 'accounting alerts', singular='accounting alert'),
        _named((facts.retirement,) if facts.retirement else (), 'retirement alerts', singular='retirement alert'),
        _named(barriers, 'service stop barriers', singular='service stop barrier'),
        _named(server, 'server recovery alerts', singular='server recovery alert'),
        _named(forecasts, 'pool forecasts', singular='pool forecast'),
        _named(near, 'near read bound'),
        _named(facts.move_holds, 'move alerts', singular='move alert'),
        _named(facts.turn_errors, 'turn errors', singular='turn error'),
        _named(facts.model_mismatches, 'model differences', singular='model difference'),
        _named(facts.unknown, 'selection unknown'),
        _named(facts.queue_unknown, 'queue unknown'),
        _named(compacting, 'compaction readings', singular='compaction reading'),
        _named(facts.group_held, 'groups held', singular='group held'),
        _named(facts.drop_held, 'drops held', singular='drop held'),
        _named(facts.cascade_held, 'cascades held', singular='cascade held'),
        _named(facts.structure_waiting, 'structure waits', singular='structure wait'),
        _named(facts.request_waits, 'request waits', singular='request wait'),
        _named(facts.raises, 'raises', singular='raise'),
        _named(facts.routes, 'on fallback'),
        _named(facts.screening, 'screenings', singular='screening'),
        _named(facts.brief_git, 'brief git alerts', singular='brief git alert'),
        _named(facts.jail_sweeps, 'sandbox sweeps', singular='sandbox sweep'),
        _named(facts.check_failed, 'failed checks', singular='failed check'),
        _named(facts.check_unknown, 'checks unknown', singular='check unknown'),
        _named((facts.cutover,) if facts.cutover else (), 'server changes needed', singular='server change needed'),
        _named(facts.operational_notices, 'tool notices', singular='tool notice'),
    )
    return Headline(_plain(lead), tuple(group for group in immediate if group.count or group.summary == '0 waiting'),
                    tuple(group for group in lower if group.count))


def details(facts) -> list[str]:
    headline = sections(facts, detailed=True)
    return [item for group in (*headline.immediate, *headline.lower)
            if group.count for item in group.details]


def _fits(parts):
    return columns(SEPARATOR.join(parts)) <= WIDTH


def _preferred(group):
    if group.short_details:
        return (*group.details, *_human_summary(group.count - len(group.details)))
    return group.details or (group.summary,)


def _short_lead(lead):
    if lead.startswith('service busy') and ': main loop stuck' in lead:
        return 'service busy: main loop stuck'
    for state in ('SERVICE DOWN', 'service ok', 'service busy', 'service unknown'):
        if lead == state or lead.startswith(state + ' '):
            return state
    return 'service unknown'


def render(headline: Headline, *, lead=None, notes=(), compact=False) -> str:
    """Reserve service state, provenance and the pointer before spending space on names."""
    shown_lead = _plain(lead or headline.lead)
    immediate = list(headline.immediate)
    later_groups = headline.lower
    named = [_preferred(group) for group in (*notes, *immediate, *later_groups)]
    full = (shown_lead, *(item for group in named for item in group), POINTER)
    if not compact and _fits(full):
        return SEPARATOR.join(full)

    front = [shown_lead, *(group.summary for group in notes)]
    immediate = [group for group in immediate if group.count]  # a quiet counter must not displace an alert
    hidden = sum(group.count for group in later_groups)
    choices = [min(((group.summary,), _preferred(group)), key=lambda items: columns(SEPARATOR.join(items)))
               for group in immediate]
    after = []

    def line(required=choices, later=after, more=None):
        more = hidden if more is None else more
        items = [item for group in required for item in group]
        return (*front, *items, *later, *((f'+{more} more',) if more else ()), POINTER)

    if not _fits(line()):
        front[0] = _short_lead(shown_lead)
    while immediate and not _fits(line()):
        hidden += immediate.pop().count
        choices.pop()
    if not _fits(line()):
        return SEPARATOR.join((*front, POINTER)) if _fits((*front, POINTER)) else POINTER
    for index, group in enumerate(immediate):
        trial = choices.copy()
        if group.short_details:
            shown = []
            for detail, short in zip(group.details, group.short_details):
                remaining = _human_summary(group.count - len(shown) - 1)
                for item in dict.fromkeys((detail, short)):
                    if not item:
                        continue
                    trial[index] = (*shown, item, *remaining)
                    if _fits(line(trial)):
                        shown.append(item)
                        choices[index] = trial[index]
                        break
            continue
        trial[index] = _preferred(group)
        if _fits(line(trial)):
            choices[index] = trial[index]

    for index, group in enumerate(notes, start=1):
        front[index] = _preferred(group)[0]
        if not _fits(line()):
            front[index] = group.summary

    for group in later_groups:
        preferred = (group.summary,) if compact else _preferred(group)
        for items in (preferred, (group.summary,)):
            if _fits(line(later=(*after, *items), more=hidden - group.count)):
                after.extend(items)
                hidden -= group.count
                break
    return SEPARATOR.join(line())


def status_line(facts, alerts=None, *, notes=()) -> str:
    return render(cache_document(facts), notes=notes, compact=alerts is not None and not _fits(alerts))


def cache_document(facts):
    """Omit detail groups too wide to fit from the published rendering choices."""
    headline = sections(facts)
    def bounded(groups):
        bounded_groups = []
        for group in groups:
            if group.short_details:
                variants = [(item if columns(item) <= WIDTH else short, short if columns(short) <= WIDTH else '')
                            for item, short in zip(group.details, group.short_details)
                            if min(columns(item), columns(short)) <= WIDTH]
                items = tuple(item for item, _ in variants)
                short = tuple(short for _, short in variants)
                group = group._replace(details=items, short_details=short)
            elif not _fits(group.details):
                group = group._replace(details=())
            bounded_groups.append(group)
        return tuple(bounded_groups)
    return Headline(headline.lead, bounded(headline.immediate), bounded(headline.lower))


def recover_document(raw):
    """Validate the optional rendering choices in a published status."""
    if not isinstance(raw, (tuple, list)) or len(raw) != 3 or not isinstance(raw[0], str):
        raise ValueError('invalid status headline')
    lead, immediate, lower = raw
    def unpack(groups):
        result = []
        for items, summary, count, *extra in groups:
            short = extra[0] if extra else ()
            if (not isinstance(items, (list, tuple)) or not all(isinstance(item, str) for item in items)
                    or not isinstance(summary, str) or type(count) is not int or count < 0
                    or len(extra) > 1 or not isinstance(short, (list, tuple))
                    or (short and (len(short) != len(items) or len(short) > count
                                   or not all(isinstance(item, str) for item in short)))):
                raise ValueError('invalid status group')
            result.append(Group(tuple(_plain(item) for item in items), _plain(summary), count,
                                tuple(_plain(item) for item in short)))
        return tuple(result)
    return Headline(lead, unpack(immediate), unpack(lower))


def bound_text(text, *, lead=None, notes=()):
    """Bound an older publication or a plain status error without slicing text."""
    first, *items = text.split(SEPARATOR)
    items = [item for item in items if item not in (POINTER, 'details: qd status')]
    groups = tuple(Group((_plain(item),), _plain(item), 1) for item in items if _plain(item))
    return render(Headline(first, (), groups), lead=lead, notes=notes)
