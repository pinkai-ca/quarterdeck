# Quarterdeck design: Post

A page of [the design](../DESIGN.md).

## 7. Post

### Across ships

A cross-ship address is `seat@ship`, where the ship is its project folder name. The fixed
`<project>/office/`, when declared as kind `office`, has the reserved address `office`.
`fleet.peers(ship)` derives this index from declarations and direct non-template folders under
`<project>/ships/`; it is shared with office/raise, never another configuration file. A standalone
ship discovers only itself. Local addresses and existing immutable message files stay unchanged.

Each service writes only its own runtime. A sender publishes an immutable offer under its own
`post/outbound/<destination>/<id>.json`, binding the source bytes, size, digest and required ancestry.
Only the addressee's service fetches it, reading the offer and source files read-only and verifying
unchanged regular-file identity and exact bytes before publishing in its own `post/store/`.
`fleet_post` atomically publishes local import provenance before each copy, uses no-clobber publication,
and then writes its own `post/fetched/<source>/<id>.json` receipt.
An equal existing file completes the same fetch after a crash; a conflicting file is retained and
reported. The sender reads that receipt and matching destination bytes read-only. A down ship has
not fetched: the sender retains the offer and names the waiting destination and age in status.
Publication requests no file or directory sync. Unchanged ticks do not rewrite completed offers and copies.

Imported ancestry is context, not another delivery request. Addressed import markers make only the
intended post eligible for the ordinary local Attempts/door/watch. Import receipts never claim seat
delivery. Local read views project this ship's qualified party names to bare names and qualify foreign
legacy ancestors by their recorded origin, without rewriting message bytes. Replies retain `re` and
travel the same way; acknowledgments never wake or close, and only the addressee's report closes an
order. A report to a silent sink also returns as context to its order's origin. Thread/inbox and
accounting readers share those same local identities, so equal bare names on different ships do not
become the same participant. All ancestry required by `qd thread` is fetched with the message.

The message JSON and local receipt schemas are unchanged. Cross-ship import/offer evidence is new;
older programs do not understand qualified routing. Do not downgrade a participating service while
cross-ship messages are pending. Forwarding old addresses is a separate change, and this transport
does not move an office or edit any live ship declaration.


An ancestor authored by the destination is an id/hash reference only: sender and fetch verify its existing destination file, and fetch copies no ancestor bytes or metadata. A missing or differing file holds the descendant. Other refused ancestors hold their descendants on both paths; refused roots never become eligible and unreadable refusal evidence holds too. Each offered origin belongs to its authoring ship; a foreign ancestor crosses only as context backed by its import provenance. A jailed sender sees peer declarations through read-only files in otherwise empty, read-only ship folders; peer homes and runtimes stay hidden.

`[forward]` maps retired local seat names to qualified addresses of another ship, for example
`desk = "desk@office"`. The mapping can retain a name removed from `[seats]`; it cannot replace a
group, operator or silent sink. Local dispatch stops using that old address as soon as its declaration
is read. The sender resolves the declared route, retains its immutable `post/forwarded/<id>.json`
record, and offers the original post with format-2 forwarding provenance. Only open orders and
questions cross to the new address; settled messages and acknowledgments keep their original
recipient. Closure is decided before applying a declaration, so an old closing report keeps its
order closed. A reserved compaction handoff and an active successor transition stay at the old
seat, including its local view. Unresolved old attempts
hold the forwarding; their evidence is read, never cleared by transport. A loop, unknown destination
or route changed after publication is retained with an operational hold. Each service still writes
only its own runtime, including when reading an intermediate ship's old attempt evidence.

Forwarded bytes retain the old `to` and original id. The receiver records the route locally before
making the post eligible. Its view resolves that one recorded recipient to the new seat; the origin's
view uses the same pinned recipient when matching the new seat's closing report. Return ancestry
carries that route too, so a closing report reaches the original order without replacing its file.
`qd thread` prints the saved old and new addresses for messages its reader sent or received. Imported
context alone never creates a delivery; an explicit addressed marker is required even when a route
returns to a different seat on the origin ship and its original file is already there. Normal traffic
keeps format-1 offers. A service without forwarding support refuses format 2 rather than treating the
old envelope as a delivered forwarded post. This declares routing only: an office move still needs
the person's separate instruction and its offline proof.

For a ship declaring `una`, the service observes the framework checkout's local
`refs/remotes/origin/main`. The first reading establishes a baseline; each later
observed tip change starts a twenty-minute deadline. Further changes collect in
one batch and move its deadline to twenty minutes after the newest observation.
At that deadline the service files one order from `ship` to `una`, listing every
observed tip in the batch. Una alone reads GitHub with the ship's key, reports a
failed run to the captain at once with its logged cause, and uses blocking
`gh run watch RUN_ID --exit-status` for a run still going. The service makes no
network or provider call. Transitions entirely between service observations
cannot be reconstructed; a restart observes the current ref against its saved
baseline, with downtime pushes timed from that observation.

The service alone writes `runtime/post/push-reminder.json`: version, last observed
tip, pending tips, UTC due time, and an optional serialized immutable order intent.
An unchanged ref causes no journal write. The fixed intent is saved atomically before
publication through ordinary post admission; recovery checks existing post bytes
and reuses the same identity before clearing it. A restart on either side of the
post write therefore neither loses nor duplicates a reminder. A missing or
temporarily unreadable ref does not discard a pending batch; invalid journal
state is retained and logged by the service's isolated duty. No new ship setting,
polling seat, timer process or change to signing is involved.
Intent recovery compares the existing immutable post before using the public
`Store.write` path for a missing post; publication hooks and recipient admission
remain owned by the store. Within a service process an unchanged unavailable-ref
error is logged once; a changed error or a return to an available reading is
logged again, and a later recurrence starts a new diagnostic.

Saving a delivery proof compares its newly merged, serialized JSON against the
current file bytes using the same locale encoding, indentation and trailing
newline as its writer. Equal bytes leave the file alone; a missing file is a
first write. Equality means only nothing to write, never durability or that an
earlier failed write was repaired or resolved. Different bytes still use
`durable.replace_file`, closing the complete temporary file before renaming it without a storage sync. Other read errors and write errors propagate;
the save publishes its merged in-memory data only after equality or successful
replacement, never as an in-memory substitute for reading disk. Repeating a
delivered finish therefore does not replace an unchanged journal, while normal
receipt and attempt reconciliation still runs.
Errors retain their journal path in the exception trace. No repair flag or
hidden same-process state changes a following pass's disk comparison. Attempt
closure likewise skips a result/detail already present in its locked on-disk
receipt, preserving first writes, terminal rules and real receipt changes.

Brief rendering also compares the actual encoded file before rewriting it,
including when a rebrief retries after another seat remains unresolved. First
renders and changed text keep their existing write behavior. Heartbeat time and
status collection timestamps continue advancing: freshness is real changed data,
even when the displayed status text is the same.

A post proof confirms storage and says only what its caller can observe. A seat's own saved launch establishes whether the managed jail hides other seats; the current declaration alone does not describe an already running launch. A missing, malformed or unreadable caller launch leaves that view unproved. In either case the proof reads no recipient launch, host service PID or watch PID: it names service delivery, or the declared human inbox route, and says that recipient/watch state is not visible. Hidden metadata is never a down or unarmed fact. This uses the existing own-seat mount and exposes no additional path, marker or environment variable. Unrestricted callers retain the normal up/down, service and watch proofs; failed recipient selection or unreachable transport is reported as unknown. Stored messages and delivery/receipt behavior do not change.

A message is one immutable JSON file:

```json
{
  "id": "ord-20261106-2101-desk-7f3a",
  "kind": "order",
  "from": "desk",
  "to": "scott",
  "re": null,
  "subject": "Codex profile of the fake harness",
  "due": "2026-11-08T18:00:00-07:00",
  "at": "2026-11-06T21:01:12-07:00",
  "body": "Build the Codex profile of the fake harness. Report when its contract tests pass."
}
```

Five kinds: order, report, question, answer, ack. `re` names the message being answered. Only a report closes an order: a report from the order's addressee whose `re` names it. An ack claims: it names the order or question its sender is working on, for the Ledger and the seat's reading in `qd status` and `qd watch`, and never closes anything. A claim ends when its message closes, by the one rule that says whether a message is open (`Store.closing`): an order by its addressee's report, a question by its first answer; any other kind is never open, so an ack on it claims nothing. It is read at the turn's end, so a later answer does not take back a turn spent before it. The same id twice is one message: every brief says so, and a message typed a second time (`qd post resend`), or printed again by a watch that had it before, carries one line above its unchanged envelope: `qd > second delivery of <id>; you may have it already: act on it once`. A receipt reads either form (`post.delivered_forms`); delivery decides nothing differently. `subject` is optional, one line. A message's sender is the seat that posted it, read from the identity its launch set (`QD_SEAT`) and never from a flag, so no seat speaks for another; a shell that is no seat posts as the operator, or names the sender it speaks for.


The forum, the cascade, the shuttle and the office are [structures](structures.md).

Where files live, under the runtime: `qd post` writes each message atomically into `post/store/`, the one canonical copy; `post/inbox/<seat>.md` is the rendered view, open obligations first, oldest first, then what was already delivered; `post/attempts/` holds one record per delivery attempt. Messages are addressed to seats by name, whatever program a seat runs.

The service walks every write and listing under its own `runtime/post` by descriptor and does not follow a link. A link is a hold that names it and is left in place; a non-regular file is refused before it is opened; a listing skips that kind at a store name, and a non-regular imports record stops the listing; an error that stops the store listing, including one the filesystem raises, and a link at `imports/`, are named on the status line; a missing parent is created as a real directory; the rendered inbox is replaced through a random exclusive temporary. Fleet publication uses this same walk.

The inbox file retains every open message in full, followed by a contiguous newest-first tail of received messages: at most 24 hours old, 200 entries and 256 KiB of rendered UTF-8 text including its headings and closing line. A received message is included whole or left in the store; an oversized newest entry leaves the tail empty. Open messages have no size or age cap. The closing line counts omitted received messages and names `qd inbox --all`, which reads the complete history from the immutable store to stdout without writing a history file. Ordinary `qd inbox` uses the same bounded reading as the file. The service already compares the prospective text before rendering; the renderer also preserves an unchanged file.

Ordinary post, attempt, journal, launch and selection writes use `durable.replace_file`: write a complete temporary beside the destination, close it, then rename it into place, without file or folder sync. Readers see the old file or the complete replacement after a process interruption; an operating-system crash or power cut can lose recent writes. Readers refuse incomplete or empty evidence. Exactly three law records keep a file sync and folder sync: the refusal quarantine and compaction transition claims (`context-is-cache`, preventing a refused message from being typed again), and the initial delivery attempt opened before typing (`one-door`, never type twice). The initial attempt costs one file sync and one folder sync per delivery; subsequent attempt updates and completion writes remain unsynced. Under `runtime/post`, `durable.replace_file` delegates to the unsynced `post_dir.replace_bytes` descriptor walk. `Attempts.open` reaches `durable._replace_law_record`, which delegates to the fixed private `post_dir._replace_law_bytes` on the same held parent descriptor, syncing the file before rename and that parent once afterward. The public post writer has no sync option. These are fixed call sites, not an opt-in or mode. SQLite also retains its own recovery-only hot-journal syncs, described in [Ledger](ledger.md#malformed-database-recovery).

The immutable post cache checks directory identity and modification/change times. Following [Git's racy-timestamp principle](https://git-scm.com/docs/racy-git.html), it trusts a matching stamp only when the cached listing itself began strictly more than two seconds after both directory times. A recent listing must be refreshed before certification; merely waiting cannot make it trustworthy. Old certified directories need only constant-cost metadata and clock checks. This guards identical timestamps around two atomic publications, rather than assuming nanosecond fields imply nanosecond precision.

The supported timestamp tick is two seconds: [Python documents coarse filesystem timestamps](https://docs.python.org/3.14/library/os.html#os.stat_result), and [Linux FAT uses two-second timestamp units](https://github.com/torvalds/linux/blob/master/fs/fat/misc.c). Certification uses epoch nanoseconds comparable with file times. Because [the realtime clock can move backward](https://docs.python.org/3.14/library/time.html#time.time_ns), samples bracket stat and listing; observed rollback, future stamps and failed reads leave the cache untrusted. This assumes local, coherently observed metadata with timestamp coarseness no greater than that tick. Coarser/delayed remote metadata or clock changes hidden entirely between observations are outside this proof.

Attempt files are mutable: each read scans their file identities, sizes and nanosecond modification/change times, reusing parsed attempts only while those match. This sees another process's same-path or same-size rewrite without relying on directory time. Failed scans or malformed files raise rather than return cached success or a partial empty reading. Both caches are in memory; their on-disk formats are unchanged. A reading or service phase derives its attempt results from one explicit inventory; journal reconciliation takes a fresh inventory after it may have changed receipts.


How a message is typed into a seat, the person's own session and the waking line are [the door](door.md#the-door).

**Delivery, exactly once or visibly not.** Before typing, the service persists the exact transport, attempt files, original attempt time, pane, process, launch, reader and owned record. It writes each paste or submit phase before the side effect, and saves the verified display before submission. Recovery never repeats a paste or submit whose outcome is uncertain. An attempt without this journal cannot claim text in the box merely because it looks like the message. The service schedules one message and journals one attempt per new transport. The persisted attempts list remains readable by preceding versions and can still reconcile older multi-attempt journals.

Only an exact, fresh, timestamped input in the owned record confirms transport. Record acceptance into a queue is `queued`, not `delivered`; its timestamped consumption must be seen before it becomes delivered. Untimed or stale identical inputs prove nothing. The same matching rule serves the door, the service and `qd check`, using each adapter's input evidence. Unjournaled open, queued and blind attempts use the same receipt rule over retained records with proven readers and their original attempt times. The open-attempt confirmation window and blind-attempt retry cadence remain distinct from journal recovery. Screen echoes, padding and a disappeared box never supply that receipt. Missing or unreadable records leave uncertainty; a malformed journal is not reconstructed or resent and does not prevent another seat's intact receipts from reconciling.

The adapter declares `record_time_resolution` in seconds. Its default is zero: every existing adapter keeps the exact comparison. Grok 1.0.77 writes whole-second row timestamps and declares `1.0`; `input_receipt.confirmed` floors its local `since` comparison to that second, without rewriting the persisted attempt time or native evidence. An identical input in the previous second is still stale. The whole payload must match, including its unique message id, and the record is already owned by that launch. The Door makes one transport attempt and never retypes uncertainty. This resolution handles the native timestamp's precision; no screen, changed payload identity or retrospective phase rewrite supplies receipt evidence.

A program whose fresh brief starts a model turn declares `opening_prompt_turn`. Its check waits for a recognized ready idle door and a known ended opening turn (bounded by 180 seconds), then requires an additional ended task turn. The check journals its single paste through the same pending Proof as ordinary service delivery. A submitted queued/pending input gets up to 180 seconds for exact record consumption, never a resend.

`qd check` retains the normal launch, paste, file, post and ended-turn proofs. Only after every normal check, including installation, passes does it add the waking step and launch its separate throwaway human-owned seat without a waiting order at launch. Earlier failure adds no waking row at all. The shared adapter contract declares nullable `waking_inapplicability`; the generic check reads that reason and contains no program-specific branch. After observing its known-empty box and absent watch, it publishes a waiting order and calls the production waking path. It requires the fixed line's owned-record receipt, a later saved watch registration, the waiting message's ordinary watch receipt and the seat's matching answer closing the order. Before temporary-seat teardown the waking reservation and a separate first-delivery/final-observation file are retained under `runtime/check/`. A check-only in-memory observation callback retains the first Door result, detail and shown box without adding fields to production wake reservations; detail and box fields are capped at 256 characters with explicit truncation flags, and the observation file is capped at 8 KiB. A missing delivery remains unknown rather than an invented result. The check-only adapter evidence hook saves a separate private file of up to eight last recognized call/result pairs and 32 KiB, from that seat's own already-pinned record with identity/fingerprint rechecks. JSONL reading is bounded to a stable 256 KiB tail; an incomplete row, unmatched result or clipped value is partial/unknown. The OpenCode snapshot reading is capped at 32 MiB for database plus WAL, selects only the existing own-root session, and retains known inputs while output/error schemas remain unproved. Unsupported native shapes, including unknown custom-tool input, are not fabricated. Content fragments, clipping and source limitations stay only in runtime/check; public check summaries expose no tool text. The hook neither requests native work nor changes Turn/State, receipts or records. Evidence failures do not replace a probe's primary exception; an otherwise returned check records a named retention failure. Obstructions or unknown receipts fail with their reason, including Grok's training question: nothing presses an account-holder choice. A check result's `passed` is true for passed, false for failed, or null only for genuine inapplicability; text renders the latter N/A and its named reason, and it is not a failure exit. Antigravity's added waking step is **not applicable: Google pool reading** and starts no human-session seat. Its existing normal checks remain required for a proven version; N/A alone establishes no proof. The normal boolean callers retain their behavior. Functional acceptance is separate from source preparation: Claude Code, Codex, pi and OpenCode on local cards, plus Grok Build, still require real checks; the fake-crew gate must also cover an order given by hand, a captain posting to an unwatched desk, one empty-box wake, watch registration and order closure.

Antigravity 1.5.5 can show a new native queue before it writes that input. `queued_input` requires a previously empty queue, one matching transport-envelope preview with complete kind/sender/short-id, the edit hint, an empty recognized box and unchanged launch, after the Door matched the whole paste. This returns `queued` with explicit record-pending detail. The journal remains `submitted` and adds optional boolean `screen_queue: true`; other work stays held. The exact receipt clears the marker and advances the existing queue/delivered phase. Queue disappearance is not acceptance. Unreadable/ambiguous evidence, original-time 600-second inspection and temporary-owner teardown retain their ordinary holds and never replay typing. Existing journal readers ignore the optional flag and still hold `submitted`. Public queue enum values are unchanged; `queued` can now describe screen-only evidence, distinguished by detail and pending phase. No Screen/State or Ledger field is added.

The Codex and Claude receipt readers skip unknown or malformed rows and continue through readable evidence on either side. Additional unrelated fields do not invalidate a known row. Codex requires its known user-message subtype; missing essential text is not an empty receipt. Known non-input metadata is ignored without being diagnosed or used as a program-format marker. Empty records remain distinct from unreadable files or wholly foreign program formats, which are reported as failures. Detailed `qd watch` gives one line of counts by bounded known kind and fixed reason. JSON gives exact totals and group counts, at most eight numbered examples, and the number omitted. Neither contains source values, and no skip history is stored. These read-only diagnostics do not change activity, obstruct the door or invalidate a proven receipt.

Claude queues preserve original enqueue times and FIFO positions. A contentless `dequeue` names no input: it is a known shape, and affected queue entries hold until actual readable input identifies them. A later matching input uses the original enqueue time, so a stale or untimed identical queue entry cannot gain freshness from a dequeue or echo. A string-content removal supplies consumption evidence, but retains an echo slot until actual input prevents that echo becoming a fresh acceptance. An unknown queue operation leaves affected state unknown; later readable input can resolve it conservatively. Identical entries with ambiguous echo history may therefore hold rather than certify a newer attempt.

Claude Code 2.1.317's native `popAllEditable` returns selected queued commands to the composer draft. Its logger writes one root `popAll` row per selected item with optional string content; `popQueuedIntoDraft` replaces the draft with the merged text. It does not deliver those commands to the model. An exact unwrapped whole-content match removes one known queued FIFO slot, retains the original acceptance/echo identity for a later actual submission, and supplies no consumed or delivered receipt. Other slots remain queued. Queued answers and reports, even though they are not open obligations themselves, remain unread and are included by restart/fresh carriage until actual receipt or the existing distinct launch-carriage proof. A later merged or edited draft is not a receipt for each former component: substring matching cannot prove delivery, so unmatched original transports remain unconfirmed.

Missing, non-string or unmatched root `popAll` content leaves identity unresolved, including repeated rows after the known slot has left the queue. The optional in-memory/JSON `State.queue_error` then gives a content-free reason; `queued` is last-observed candidate history, not a current count or membership assertion. This uncertainty persists for that record, independently of known turn/tool/completion facts; later deltas and idle screens do not establish a complete queue snapshot. A different record has its own reading. Compact/full watch and the status line expose the uncertainty (including a separate queue-unknown category on status overflow). After-order automatic fresh, pool selection and failover require a proved empty error-free queue and hold with the reason visible. Explicit live restart/fresh requires the queued collection to be known empty and a real timed completion, but a residual queue identity error alone is not a permanent veto. Explicit stop/down and the question-only recovery exception keep their existing rules. Endpoint-only recovery checks cannot see Claude queue uncertainty and are unchanged. Sidechain draft-return rows do not alter the root queue or its uncertainty. Native turn/tool completion still requires its own recorded evidence.

A matching older pending slot, left by identity-free dequeue or an unknown operation, is a possible native queue member. A pop cannot skip it to select a newer identical known slot: both receipt and state readers leave candidate/acceptance identities intact and report the pop unresolved. Echo and returned-draft slots are not native queue membership. Automatic status-line queue collection reads only nonhuman Claude records. Human-owned status alarms retain their metadata-only boundary and do not claim to observe human queue state; explicit requested watch/full readings can expose it.

An unresolved submission on an old launch stays visible without owning a different launch's empty, recognized door. If an explicit lifecycle operation carries the message into a later launch, its distinct `launch` receipt supersedes the old hold without declaring the old submission delivered. A changed launch alone proves nothing. After inspecting the box and owned record with `qd watch <seat>`, the Desk may deliberately request `qd post resend <id>`. That request can retire prior uncertainty only with an empty, recognized box; it cannot clear a draft, repair historical evidence or declare the earlier attempt delivered. Ordinary ticks never resend an unconfirmed submission.

An unresolved journal may instead acquire terminal phase `not-in-closed-record`, displayed as **not in its closed launch record**. This is an attempt reading, not proof that the program never acted. All three facts must be readable: the exact old launch is closed by ordinary lifecycle evidence (its pane is absent from the server, a later saved launch exists, or the server began after the matching launch file was written); its exact saved record has an unambiguous retained reader and readable input evidence with no skipped, unknown or incomplete input rows; and no input at or after the original attempt time matches the payload under the shared text rule. Queueing and other input dispositions still establish presence. Unknown timestamps, failed tmux queries, empty/missing/unreadable records and ambiguous reader bindings hold. Initial supported readers are Claude Code, Codex and pi; other readers hold.

The same rule reads old journal shapes, without backfill, a new baseline or a new proof artifact. Settlement preserves the original time, payload, reader, record and attempt files, changes neither native records nor message openness, and authorizes no transport or automatic fresh start. Ordinary ticks cannot replay the terminal attempt; normal open-thread carriage on an explicit later launch remains available. Repeated reconciliation is idempotent, including recovery after saving the terminal journal but before updating its attempt. Sticky `unacted` and `uncertain` results retain their separate rules. No Ledger schema changes; a version predating this terminal phase reports the unfamiliar journal as unreadable and must not be used to reconcile it.

Finished delivery evidence leaves the working folders after ledger catch-up. Once a message is closed, its latest delivery is confirmed (`delivered` or `watch`), and every attempt is finished, the service may move its old-launch attempts and settled journals unchanged into `post/retired/attempts/` and `post/retired/pending/<seat>/`. Current-launch receipts, open messages, uncertain or queued inputs, active journals and the service's in-flight delivery pointer stay in place. A lifecycle claim, recipient admission and the attempts lock exclude concurrent delivery and launch changes. Unknown journal or ownership evidence keeps that seat's files in place. Each pass moves at most 1,000 attempts and 1,000 journals, at most once a minute, after the operational duties and accounting. The duty checks a one-second monotonic work budget during file scans and between moves, updating the heartbeat at 0.1-second progress points. Budget expiry yields ordinary partial progress for a later pass, whose seat cursor continues after the last seat inspected; filesystem calls and scheduler pauses can overrun this cooperative budget, while the independent service heartbeat thread remains active.

Before moving any attempt, the service publishes an immutable metadata batch under `post/retired/index/`. `Attempts.all()` merges those cached batches with the active folder; an active original takes precedence during an interrupted batch. Historical latest/accepted results remain visible to delivery, carriage, compaction, watch and status without statting every retired file. Attempt numbering includes retired names. Named attempt and journal readers resolve the original location or its deterministic retired location; retired receipts are not rewritten by a late completion. Each pass publishes one immutable batch index before moving files. It includes the intended attempt metadata and journal hashes. Journals move before their attempt files, by same-filesystem rename; after the renames, the pass records completion under `post/retired/completed/`. Index and completion records use a temporary file and rename, and retirement makes no file or folder sync calls. A yielded partial batch retains its unfinished intent. The next pass finishes an unfinished intent before planning another batch. A process crash between renames or before completion leaves every original wholly in one location; replay completes the moves and records completion. These writes do not promise persistence across a power cut; the operating system decides when they reach storage. The index is not rewritten, and a pass with nothing to move writes nothing. No attempt, journal or post is deleted. Ledger attribution reads immutable posts and owned native records; future catch-up still has all receipt history and named originals available.

**Attribution.** One change has one concrete order. Its implementation, test and correction orders refer to that order with `re`; reports still refer to the assigned order they close, and answers to their question. Review and merge messages remain in that same explicit reply thread. An umbrella campaign is not the anchor for its separate changes. The framework follows the links, never subjects, quoted ids, role names or an inferred parent. Historical messages without links remain independent or unassociated; semantic grouping of that history is evaluation work outside the framework.

For each recorded turn, the Ledger collects valid posts made by that seat within the turn and exact complete post envelopes consumed in its record. A consumed queue entry needs its consumption time. Acknowledgments must claim the sender's own open order; only the first actual closing report supplies closing evidence. Duplicate/foreign reports and invalid claims do not create work. Missing, conflicting, cyclic or time-inconsistent threads are unresolved. Direct evidence takes priority over fallback context: with no direct order evidence, use a valid claim active at turn start, else the sole open-order interval overlapping the turn. Tied latest claims or several competing open orders are uncertainty, not proof that each was served.

Several directly evidenced change roots in one turn are shared work. The turn's measurements are counted once under `[shared]`, with the candidate roots and evidence in the reading; no token fractions are invented and no last message wins. `-` means no association in available evidence; `[unresolved]` means an evidence gap or competing candidates. `qd ledger --by order --json` gives the basis for every turn, including fallback context. These labels describe attribution, independently of the measured/unknown usage figures.

Claude's watch route is separate from transport receipts: a recorded `qd inbox --wait` command names an output file, and a uniquely linked recorded `cat` or complete `Read` result supplies the full envelope at the read-result time. The output file itself is never opened for attribution. Arbitrary stdout, compound shell reads, conflicting/reused output origins and truncated envelopes do not prove that route. Launching a watcher or its completion notification alone does not charge a later message to that launch turn. Incoming evidence that overlaps turn intervals, or a posted message that could belong to overlapping records of the same seat, remains unresolved.
