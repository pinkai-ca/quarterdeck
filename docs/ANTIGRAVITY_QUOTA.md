# Antigravity quota evidence

The adapter reads two distinct facts. A quota error proves an observed wall; a successful
`/usage` result measures remaining quota. An idle prompt, a later input, or a successful
usage read does not erase an active observed wall.

## Observed walls

The 1.5.2 individual-quota banner is recognized even when the input box is empty.
Bounded wrapped continuations are normalized through the error ID, stopping at input
and frame boundaries, so terminal reflow preserves reset and event identity. The
owned SQLite record is canonical: `steps.step_type=17` is `CortexStepErrorMessage`,
`step_payload` field 24 contains that message, and its field 3 contains
`CortexErrorDetails`. Fields 1 (`user_error_message`) and 3 (`full_error`) carry the
quota notice. The metadata's `created_at` anchors the relative reset. Neither file
mtime nor a later input supplies an event time. An unavailable time/reset stays unknown.
Session logs are cross-check evidence, not an automatically scanned source: a global
log cannot establish a seat's record ownership or the pool of an older error.

The ledger retains the source record path, step index, error ID when present, a digest
of the notice, original event time/reset, first observation, seat and launch pool.
New events use the accepted per-record launch journal. Unknown or conflicting history
is displayed as an unknown pool and holds that seat, without charging another pool.
A known wall follows its original pool across a resume or pool switch.

Screen-only evidence uses the first observation of that launch/error; repeated polls
cannot move its reset. Door and ship-wide wall text label this reset as estimated from
first screen observation. A superseding canonical record shows its event-anchored reset
without the estimate label; an unknown reset remains unknown. A matching canonical
record supersedes that fallback, including when it proves the displayed banner has already expired. Persisted matching evidence
also recognizes that banner after a restart while the record is unreadable. Disappearing banners, record
read failures, and idle screens leave persisted events intact. Known expired events do
not block or trigger another refresh. A reset with no reliable timestamp remains
unknown and is not cleared by elapsed silence.

The Door checks persisted walls before typing or interrupting to deliver. Ship-wide
status lines read the same ledger and display the wall/reset in every session. The
status/watch path never requests authenticated quota data.

## Qualified usage command

The measured command is exactly `agy -p /usage --output-format json` on 1.5.2, with
`AGY_CLI_DISABLE_AUTO_UPDATE=true`. Its supported-command path branches around
`session.start` and the turn runner. The qualification audit observed an empty
conversation ID, zero reported turns and all five token counters zero. File syscall
tracing and before/after metadata inventories independently showed no conversation
file opens, writes or new join candidates, including transient files. Directory and
file metadata enumeration did occur. This proof is specific to the audited executable
bytes, not a promise about every version with the same version string.

The command has operational writes: a session log and its current-log symlink, the
conversation-summary database and temporary WAL/SHM files, a temporary crash log, and
onboarding-cache replacement. These are the program's own writes, authorized for this
usage read. The framework does not edit credentials, sign-in settings or source
conversation files. Private raw audit evidence is kept in the ship runtime, never in
fixtures. Tests execute no real usage command.

Before invocation the reader hashes an opened executable and verifies its identity
against the qualified hash. It executes that same descriptor, so a pathname replacement
cannot select different bytes after verification. Changed/unqualified bytes produce
visible unknown usage with a requalification reason **before execution**. The exact
argv and automatic-update suppression are retained. A failure, timeout, malformed
result, unexpected conversation ID or nonzero model work also yields unknown usage.

## Windows, units and refresh

`command.data.groups[].buckets[]` supplies group, bucket ID and name, window,
`remaining_fraction` and `reset_time`. A finite numeric fraction in `[0,1]` maps to
`Window.used = 1 - remaining_fraction`, also on `[0,1]`. Booleans, missing values,
amount-only data and malformed fractions do not establish a percentage. Invalid or
unqualified timestamps remain unknown. Duplicate decision buckets are ambiguous.

Gemini 4 Argon's weekly bucket is the Google pool's `7d` decision window. All other
returned group/window buckets remain visible with their own identities and resets;
exhaustion in Claude/GPT or the other Gemini group does not automatically block Argon.
Missing/unknown Argon usage cannot establish a pool as available for automatic
failover. A complete real reading requires both a valid fraction and an aware reset
after its aware reading time; a partial tuple is never a current measurement.

The service refreshes every declared Google subscription pool every five minutes,
including with only Codex seats, all seats down, or no seats. A new active wall adds
one refresh. Scheduling follows declared pools rather than the currently running harness. The attempt time is
persisted, so service restarts do not reset the cadence; failures are paced too. Expired
historical events and duplicate observations do not request another refresh. Other
providers retain their existing cadence. Status reads only the resulting cached
headline; ledger pool output includes all scopes and named read failures.

Only the default local sign-in has a qualified account binding. Google pool names with
no `config_dir` refer to that same sign-in and share one read when due together; they
are not separate authenticated accounts. An explicit account directory yields visible
unknown before any invocation rather than charging default-account usage to it. API-key
and other-provider pools do not receive this subscription measurement. No account
selector or credential-file mutation was qualified by the command audit.


## Latest real readings and failed attempts

The ledger keeps one latest complete real reading per pool/group/window, not a
reading history. Its fraction, reset and reading time are replaced together only by
a valid complete response. A missing group/bucket, invalid fraction/reset, failed
command or ambiguous duplicate updates that window's latest attempt/error metadata
without deleting or changing its prior measurement. Valid siblings update independently.
A never-successful window stays unknown, without an invented zero or reading time.
The schema migration preserves existing measurements and identities.

A retained measurement is labelled **LAST KNOWN**, with its original reading date,
age and latest failure reason, in the Google status/watch headline and ledger output.
An expired reset or reading at least an hour old is also dated LAST KNOWN; reset times
are never reanchored. A later valid reading replaces the complete tuple and clears its
error. The one-hour availability bound is unchanged; it is separate from the five-minute
background refresh cadence. Neither status nor automatic Google pool selection starts
a provider query. Failed/partial results persist their attempt time and diagnostics once,
so service restarts and selection cannot bypass cadence or overwrite per-window errors.
For other providers, an inactive fallback with a failed/incomplete measurement may
retry after the existing one-minute provider interval, measured from its latest persisted
attempt. Its retained good timestamp does not postpone recovery for an hour. A current
complete successful cache still avoids a query; key/no-usage pools keep their existing
selection behavior.

A known bucket ID cannot be replaced by a different non-null ID for the same logical
window. Such a response keeps the original tuple/ID as LAST KNOWN and reports that the
new reading could not be attributed to that window. A later matching-ID reading can
recover normally. Missing IDs never erase the established ID; a window without an ID
can acquire one on its first unambiguous complete reading. Rejected numeric tuples are
not stored. Genuine provider identity rotation needs evidence for a future identity
rule; it is not guessed from different numbers or names.

Only current, complete decision windows can establish automatic availability or a
measured exhaustion wall. Last-known values cannot do so. Nondecision group failures
do not make a valid Argon reading stale. Persisted observed quota walls remain separate:
a failed read, positive value or expired cached reading cannot remove them.
