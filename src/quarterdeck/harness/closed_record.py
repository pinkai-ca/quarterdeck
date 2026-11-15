"""Read whether an unresolved payload is absent from its now-closed launch record.

This is an attempt classification only. It grants no transport, closes no post,
creates no lifecycle evidence and never substitutes another record or reader.
"""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .. import trusted_files
from .. import harness, input_receipt, record_owner
from ..tmux import TmuxError
from .types import Inputs

if TYPE_CHECKING:
    from ..pending import Proof
    from ..seat import Seat


PHASE = 'not-in-closed-record'
DETAIL = 'not in its closed launch record; original submission unconfirmed; never resent'


def _closed(seat: 'Seat', identity: dict) -> bool:
    launched = seat.launched()
    if launched is not None and launched.when > datetime.fromisoformat(identity['launch']):
        return True
    if seat.tmux.pane(identity['pane']) is None:
        return True
    if (launched is not None and launched.pane == identity['pane']
            and launched.pid == identity['pid'] and launched.at == identity['launch']):
        return seat._earlier_server()
    return False


def not_in_closed_record(seat: 'Seat', proof: 'Proof') -> bool:
    """All three facts must be positively readable, including old journal shapes.

    Launch closure follows ordinary lifecycle rules. Retained ownership binds the
    exact saved record to its saved reader. Only a diagnostic-bearing input list
    with no skips can describe absence; unreadable/unsupported/untimed evidence
    remains uncertain. Queueing or any other input disposition is still presence.
    """
    try:
        data = proof.data
        if not data['record'] or not _closed(seat, data['identity']):
            return False
        record, name = Path(data['record']), data['identity']['harness']
        names = {row.get('harness') for journal in (trusted_files.READERS, trusted_files.LAUNCHES)
                 for row in record_owner.events(seat.trusted / journal)
                 if Path(row['record']) == record and row.get('harness')}
        if names != {name} or name not in ('claude', 'codex', 'pi'):
            return False
        reader = harness.get(name)
        inputs = reader.inputs(record)
        if not isinstance(inputs, Inputs) or inputs.diagnostics()['total']:
            return False
        since = datetime.fromisoformat(data['at'])
        forms, ignoring = input_receipt.message_forms(reader, record, data['payload'], since)
        for item in inputs:
            if item.at is None:
                return False
            if item.at >= since and input_receipt.same_input(item.text, forms, ignoring):
                return False
        return _closed(seat, data['identity'])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, TmuxError):
        return False  # no default zero, guessed reader, or missing-pane error becomes absence
