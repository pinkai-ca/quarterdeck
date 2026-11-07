# Record ownership across harness changes

A seat's selected harness describes its next program, not the reader of every record
it has ever owned. A saved pin is returned only when its recorded reader matches that
selection and any saved launch agrees. A changed or unknown reader yields an absent
current record with a reason. Fresh launch replaces the pin, retaining the old file.

`launch.json` now records the harness and paths present before launch. The durable
`record-launches.jsonl` journal includes the harness beside the existing record, time
and pool boundary. `record-readers.jsonl` preserves each owned path's reader even after
the launch file or current pin is removed. Missing files remain in that registry.
Conflicting reader evidence is unknown; a current preference never overwrites it.

Legacy launch commands can establish a reader from the exact executable or the fake
module invocation. A legacy down seat with only a bare pin or an untyped ownership
list has no reliable historical harness evidence. Its path remains owned and its
accounting is visibly incomplete until historical reader evidence is available.
Neither file suffixes, current pool preferences nor trial decoding select a reader.

Accounting, watch token reads and queued-message recovery use the retained reader for
each file. Existing per-turn launch/pool attribution and quota-event provenance remain
in force. Previously ingested rows are retained when a source becomes missing or
unreadable; completeness notes expose the gap. A new program's record cannot claim an
old program's quota event. Unknown old usage does not become zero.

Private seat-home record discovery uses only historically established adapters. Shared
Antigravity candidates are never decoded or adopted to infer ownership. A stale pin
from another harness does not resolve shared-state join ambiguity. Each new launch
excludes paths already present before it from join candidates, so corrupt historical
files remain reportable without preventing the new program from joining a new record.

JSON readers now report missing, non-UTF-8, malformed or unsupported records as explicit
record errors. Structured field errors are contained at the selected parser, leaving
other seats visible. The existing tolerance for one unfinished JSON tail after complete
entries is retained; this change does not reconstruct an unfinished entry's fields.
Tests use synthetic records and private fake processes; no real conversation, account
or credential is needed to validate the lifecycle.
