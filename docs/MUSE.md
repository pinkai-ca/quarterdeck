# Muse contract facts

Muse is registered as harness `muse`, native company `meta`, for a subscription
pool. The measured program is Muse Code 1.4.20. The adapter reads the selected
installation's version marker and runs its versioned executable directly;
it never executes the updating launcher. An executed version observation, not
the marker or an executable hash, establishes the installed version.

## Native home and sign-in

Measured native paths follow the seat's explicit HOME and XDG variables.
The adapter owns `user-home`, `config/muse`, `data`, `cache`, `state` and `tmp`
below its runtime home. The session index, records, attach paths and command
socket were observed under `XDG_DATA_HOME/muse`. No adapter-owned bubblewrap
mask remains. Where the ship declares a jail, the ship's existing jail is the
only jail.

A subscription pool's `config_dir` names its native Muse config folder.
`auth.json` is linked, never copied: the framework's credential link in the
seat home is also reached from `config/muse/auth.json`. A native usage call
rewrote the source file through the link while leaving the link in place.
That observation does not establish token-refresh values or expiry semantics.
No freshness or grant-expiry fields are guessed. A detached native credential
is retained and launch refuses pending review. If either credential name becomes
a regular file or the nested link stops reaching the pool's file, check teardown
and the service keep the home for human review, including for jailed seats.
Jailed seats stay outside pool reseeding.

The status line points to `qd credential release <seat>`. Take the seat down
before running the command. The operator runs it from a shell with `QD_SEAT`
unset and may choose `keep` or `remove`. A declared systems seat may choose
`keep` for a seat in a crew session. Other seats are refused before the command
inspects the home. The review shows the file, link, or missing state of each
credential name and the target of each link.

Choose `keep` to leave the home in place and clear the hold before the command
returns. The service can hold it again if the home or credential metadata
changes. A kept detached credential can still need attention before Muse will
launch. Choose `remove` to move the reviewed home, or the entire seat folder
for a temporary seat, intact into a private mode-0700 directory under
`runtime/evidence/credential-release/<seat>-<time>/`. The result, audit and
status name the recovery location. Any later deletion is a separate decision.

The audit is in `runtime/trusted/<seat>/acts.jsonl`. If a move fails, the review
hold stays. If the move succeeds and recording fails, the recovery location
stays visible in status. If recording `keep` fails, the review hold stays.

The launch removes inherited `META_API_KEY`, `MUSE_AUTH_PATH` and `MUSE_LOGIN`.
Only this adapter's launch removes these sign-in redirects; other adapters keep
their existing environment policy. Direct version/help measurements used an
empty private home. No sign-in values or account settings belong in fixtures.

## Model, settings and opening input

Muse requires an explicit noncontributor model. `muse-spark`, its versioned ids,
and every id ending in `-contributor` are contributor models and refuse in every
declared settings layer, even when another layer would override that choice.
Checks select exactly `muse-wonder-1.0` with `minimal` effort; this supplies no
ordinary-seat model default. A different explicit check selection refuses.

Every home, including a private version-observation home, receives
`schema_version: 1` and `contributor_mode_enabled: false` before execution.
The launch also declares contributor mode off, approval judge off and native
sandbox off. Wonder with this private setting showed contributor mode off on
its first idle screen. Spark had shown contributor mode on despite the off
flag; flags alone never prove the effective state. A contributor-on screen
holds all input. The setting is named in the launch notice.

Muse starts without a prompt. Its launch-opening journal is bound to pane,
process and launch before publication; only that verified owner can bypass
its own opening hold for one ordinary Door delivery. Carried attempts close
only on exact confirmation. The service publishes the reserved opening and returns;
subsequent passes observe startup once, submit once through the Door without
polling, then read the record once per pass. The CLI may wait for startup and
confirmation. Both paths use the same journal; neither retries a transport.
A failed or unconfirmed opening remains held,
without retry or a second queue. Startup observation is bounded by the normal
startup deadline.

The startup proof is either an explicit contributor-off idle screen, or a
later idle launch of the same previously proved home with the unchanged
framework-seeded contributor-off setting and a noncontributor model. Native
addition of the measured disclosure-acknowledgement flag is the only excluded
settings change. Every other seeded field remains bound. An on notice always
holds. The actual proof is appended to the saved launch notice. Absence of the
banner alone is insufficient.
Resume restored an echo record, but resumed brief carriage remains unproved
and refuses. Native model/effort changes have no guessed slash command.

## Record and screen

Records enumerate only `data/muse/sessions/YYYY/MM/DD/<session>/session.jsonl`.
The native session identity must match the record folder. Current typed rows
use outer schema version 1; accepted and materialized input use payload schema
version 1. The reader retains native text and timestamps, never mutating a
record. A single refill text block and a single model-message text block can
supply exact input evidence. Multi-block rendering is not invented.

A root `runtime.session` event starts with `event.kind=started` and a prompt.
Reminder tasks can also emit started/completed events; they do not replace the
root prompt or close it. A matching root's `event.kind=terminal` closes its
turn, retaining its textual reason or, if absent, its terminal kind. Successful
measured runs used `terminal=completed` and a null reason; other terminal kinds
are endings without an invented success verdict. Reply text and
`model_completed` precede completion. The native Double checking phase stays
busy while reminders finish.

Live `model_completed.model` supplies the exact recorded model. Tool-call
names are readable; complete tool failure semantics, child counts and token
cache/reasoning arithmetic remain unknown. Echo zero tokens are not native
subscription quantities. Accepted/materialized linkage uses explicit native
record/sequence/session/run links, never equality guessed between identifiers.

The 120-column styled fixtures distinguish an empty composer, a draft, working
and queued input. Indexed colors identify the prompt and typed text. The
composer sits between its full-width rules, separate from transcript prompts.
An all-space composer remains text unless the Door's bracketed owned frames
show a visible cursor at its first input cell, after the measured two-cell
prompt. That positive cursor proof reads the native painted padding as empty;
it does not strip spaces from a draft.
Unrecognized styles/layouts report unknown activity. Existing echo queue
observations establish a later distinct turn, not active-run delivery; working
delivery remains held until its record receipt is implemented.

A separate echo-only sample proved exactly one final LF removed from a
12-line brief-like payload, with a literal wrapped draft and no collapsed
marker. That single sample does not establish post-like bodies, multiple
terminal LFs, 80-column wrapping or collapsed counts. The current source
therefore admits only the measured short ordinary single-row form before any
paste: printable ASCII, at most 55 characters, at the 120-column composer.
Other forms are named unsupported and withheld, pending the rest of that
measurement. No old version's projection is borrowed. Literal input retains
every character and collapsed evidence is unmatched.

## Allowance and remaining proof

The native command is exactly `muse usage --json`; TUI flags do not prefix it.
Measured schema version 1 gives `usedPercent` and `resetsAtMs` in `window` and
`weekly` buckets. Percentages become fractions; reset timestamps are epoch
milliseconds. No short-window duration is guessed. Account/catalog labels are
ignored. Absent or unreadable allowance stays unknown, never zero used.

The decoder is pure; native periodic usage execution is not yet enabled.
An explicitly requested minimal check reads `usage --json` before and after
its one turn in its built private home. Only decoded native buckets are saved
as private check evidence; raw responses and account labels are discarded.
The shared usage owner preserves one pool reader, an absolute 1800-second
minimum interval and persisted refusal pacing. No destination gate performs
a demand read. A whole installed check has not been run. It requires separate
release after the observed material allowance cost is reviewed. Synthetic
source/tests spend no allowance. Una owns the eventual live check.

Still required: independent deferred-opening gates, current paste projection,
migration of the old mask gates, auth-link review, the mechanical watch
unit-test union check, the combined whole suite, review and exact-tip signature.

The full brief and index remain unsupported by the measured single-row input
form and hold without typing. A minimal check instead owns a fresh temporary
seat with an empty brief and the exact opening `Write OK to proof.txt.`:
22 printable ASCII characters, without a final LF. Its working directory is
the private check task folder. The existing launch-opening journal and Door
confirm that one paste by the record. The check then verifies `proof.txt`,
exactly one ended root turn and the requested model, and sends no second
order. This exception exists only for the owned temporary check lifetime;
it changes no crew brief or ordinary launch. Its native proof remains pending.

## Shared interfaces

The adapter's obstruction methods default to no action in every other harness.
Unknown activity stays unknown through Door, watch, service and interrupt
callers. An absent pane reads off even if its last launch remains saved;
an unreadable live pane still holds. The follow view starts from not working
before a fresh launch's first record, so its first turn is shown. Relaunch and in-session
controls require an explicit idle answer. Main's unread-screen journal evidence remains in front of the
unknown-activity hold. Exact receipts retain the current launch's join
provenance and every delivered form recognized by the shared receipt owner.
Screen activity before a record is joined is an explicit Muse capability;
other programs keep their record-first startup reading, with admission still
decided separately by the Door. Notice dismissal requires known activity in
both frames. An adapter may declare only its exact measured composerless
startup choice as an exception, using Down or Enter; Escape is never sent on
unknown activity. Unknown post-paste activity retains the journal's
unread-screen and short-pane diagnostics without clearing or submitting.

The adapter also owns the exclusion from unattended daily live checks. The
daily scheduler reads that declaration rather than containing a second table
of program names. The exclusion and its report text are unchanged.

Strict usage pacing is opt-in. Its persisted refusal deadline lives beside the
existing attempt table, retains the absolute minimum interval, and bounds
malformed native retry values. It introduces no provider request at a
destination gate. A standalone usage writer rolls back incomplete attempt/hold
writes, so a later commit cannot publish an attempt whose hold failed. The
module inventory is in [Modules](MODULES.md).
