# Quarterdeck design: Watch

A page of [the design](../DESIGN.md).

## 8. Watch

Watch answers two questions independently: what the record says a seat is doing, and whether its screen lets the door deliver. It reads the record first and screen second. A missing observation is unknown with a reason, never a default reported as fact. Titles and personas are display; pane, process and record establish identity.

### Activity and door

| Activity | Evidence |
| --- | --- |
| idle | Last turn ended, nothing queued. A never-active seat is not stalled. |
| working | Open turn with steps still arriving; name its open tool and elapsed time. |
| stalled | Open turn with ten minutes since the last step; keep the tool named. |
| off | No running program in the owned pane, with the observed exit/crash/missing-pane reason. |
| refused/failed | Ended turn with a reader-recognized failure kind/code; never relabel it idle. |
| unknown | The reader could not establish activity, including an unreadable saved record pointer. |

The door is open only with a known empty box and no obstruction, including a working program that can queue. Obstruction names what clears it: a known dismissible notice, time for startup/limits/nonqueueing work, or a person for a draft/login/unknown dialog. A recognized blank/startup frame reads `starting, waiting for the input box` without a key; missing UI on an unfamiliar nonblank screen still needs a person. Startup never overrides recorded activity.

Text the door did not type is left untouched. It waits in the inbox and raises the seat's human-attention alert; it is not lifted, restored or sent. A scrolled pane can remain open where the measured transport preserves scrollback. The [door](door.md) owns paste verification, pending input, uncertainty bounds, layout and waking rules. [Harnesses](harnesses.md) owns version-specific control dialogs and native screen grammars.

`qd watch` prints the crew table; `qd watch <seat>` gives the full reading. `--save-screen` captures a scrubbed fixture for investigation. Screen-only queued evidence is not a receipt. An unreadable/empty saved pointer reports its join reason; a seat that has not yet obtained a record keeps its initial idle reading.

### Sign-in failures and recovery

Claude Code 2.1.317's measured root API-error authentication_failed envelope becomes `sign-in failed in the program`; native turn duration remains completion evidence. Quoted login prose, screen text and unsupported forms do not classify it. A standalone recognized error can be displayed without inventing an input/turn. Automatic human-session status uses service-published metadata, never conversation bytes.

A live crew seat with a valid pool and just one latest sign-in-failed ended turn can take its next waiting message normally within fifteen minutes. The program rereads changed sign-in files for new requests; the failed input stays in conversation. A repeated failure, expired/unknown sign-in or expiry without a message holds the door again. `sign_in_failures` preserves each affected seat's fixed reading even on status overflow.

After credential reseeding, guarded resume requires local valid sign-in, saved paying pool and stable credential generation. First attempt needs a generation modified after launch; later attempts need a different generation from the last attempt. At most three saved attempts, sixty seconds apart within fifteen minutes of the first, live under `post/auth-relaunch/`. Reservations precede lifecycle work; held/failed requests consume the attempt and never bypass identity/idle checks. Safe logs name the held check or error kind without exception text.

A valid pool may wait up to fifteen minutes for renewal, displaying `sign-in: waiting for the renewed pass`; expired/unknown credentials call a person immediately. Window expiry or a proved new failure after the third attempt needs attention. An old failure visible while resume starts remains waiting in that window. Only adapter-proved successful completion after the last attempt ends the episode, not merely an open turn. Claude's successful fact requires a root non-API-error assistant response closed by native duration; unknown or interrupted responses cannot clear it. Human-session seats receive alarms, never automatic recovery. Older readers lacking this failure kind lose its presentation on rollback.

### Model observations

Running model is read from the owned record first, then an adapter-proved screen identity. Comparison uses the effective pool/seat choice and that adapter's identity rule; default selection invents no expected ID. Only a matching pending model control suppresses its own temporary difference. Unknown observations neither create a difference alarm nor clear a previously proved one.

A proved difference produces fixed report intents for up systems recipients, with saved deduplication. With none up, the status line retains the seat/difference even when crowded. Journals hold canonical model names and operational intents, not native captures. Visible startup model/effort refusals remain separate from declared and observed values; they neither rewrite the declaration nor change the model automatically.

### Alarms

Every session independently shows withheld/blind delivery, human attention, held limits without eligible destination, disk pressure, grant expiry and service failure. The line carries operational alerts; detailed status/ledger carry provider allowance figures.

An unknown-screen alarm needs two fresh identical complete captures in one collection, with unchanged launch/pane/process. A changed or missing second frame leaves the first reading closed and inspectable without confirming the alarm. This suppresses transient redraw notifications, not obstruction or transport checks. Known dialogs, drafts and pending uncertainty keep their own timing.

The service reports seat/reason from ship to up systems seats. Person-only cases, including sign-in and uncertain paste/submit, also reach the desk. `PERSON_ONLY` is the shared reason set. An obstructed systems seat is not told of its own door. Without an available systems seat all alarms go to desk. Each addressee is notified once per unchanged reason; clearing/reappearing or a new recipient creates a fresh notice. This deduplication is process-local, so restart may notify again. The independent status line always retains the alarm.

### Following work

When a record moves from closed to open after the service baseline, a nonhuman session can select that seat's window. Restart's first reading is a baseline, never a start. Selection checks the ship's session ownership and the launch pane/process. It refuses if any pane is in a mode, an attached client typed or used a mouse within thirty seconds, or selection would resize the target. Nothing is typed, closed or resized.

New seat windows copy the shown window's size while their placeholder runs when that session has a viewer, then unset the temporary size option. This lets later follow preserve size without pinning windows. With no viewer or uncertain sizing, normal tmux sizing applies. Existing windows are never resized. Human sessions are never followed.

### Independent status presentation

Tmux runs `qd status --line` on its own timer, independent of service and doors. Kernel service-lock ownership proves liveness: late heartbeat means busy, missing/invalid heartbeat unknown, released lock down. Up restarts a dead service. A blocked desk door cannot hide a service alarm.

The service atomically publishes `runtime/status-line.json` for text status-line jobs, dating collection start (`wall`/`monotonic`) and the publication write (`written`) separately. The optional collection-age cache retains its three-second wall/monotonic bounds and owner/declaration/framework checks. Outside that cache, a publication from the current service with an ok lead reads plain for up to `STALE_SECONDS` after its write, however long collection took. Older or unverified publications, and a busy service, retain their operational alerts under an explicit `status stale` marker and write age for at most sixty seconds. Beyond sixty seconds the command collects current facts itself and adds `publication <age>s old, collected here` to the lead. A down service is independently named `SERVICE DOWN` and also takes the collector. Missing, corrupt or undated publications, or an unknowable write age, take the collector too, so reuse can never be unbounded. The collector keeps its existing error/unknown handling. Slow collections still publish; failed collection discards old rendering. Receipt, lifecycle, usage and JSON readings still collect their own facts and never consume this file.

Presentation demand uses a fresh client listing for declared sessions. A positive no-viewer listing skips collection; unknown demand still permits it. Between duties the service checks cheap status-input hints at most every 0.7 seconds, measured from completion, and collects again when a hint changes. Hints cover post arrivals, attempts, journal folders, seat metadata and current record/WAL fingerprints, plus the service lead and accounting alarm. They schedule presentation only and never authorize an action. Unchanged inputs get a full reading on a five-second cadence; native screens, clock-based alarms and disk space are read again on that cadence too. Slow collections always leave at least one check interval before the next collection; they do not return already overdue. A change during collection remains owed, and failed collection does not renew the last rendering. The service rests after a pass until a post/request arrival or its ship tick, without collecting status inside that rest. Missing directory notifications retain the tick fallback. Withheld deliveries still get a pass after one tick, independently of presentation demand. The independent tmux command retains its current owner checks and down/busy/unknown facts. No hint, client or deadline grants lifecycle authority.

Within one read-only presentation scope, post-store instances share immutable messages/replies and per-seat projections; attempt readers share one complete inventory; human-wake presentation uses that same view. Import/forward metadata and their directory inventories are observed once per scope, including absence, and refreshed in the next scope. Service alarm presentation seeds the view with its existing readers so their ordinary validated parse caches can be reused. Native readers may share stable state/turn results. Reuse requires the existing two-second metadata certification, file/WAL identity and no source error. Changed, recent or unreadable evidence takes the ordinary reader. No observed snapshot survives its reading or wraps an action. Human-wake delivery reuses validated reader objects across its metadata, candidate and alarm readings, but each metadata/status reading gets a new snapshot and reruns the ordinary store and attempt checks; ownership, watch-generation, native-state, box and receipt checks are unchanged. Certified measured zero token totals avoid unnecessary history reading only with matching reader/launch/source/framework evidence, never by registering ownership or writing Ledger state.

JSON-line reader snapshots are bounded to sixteen records and 128 MiB raw source, with parsed state beside them. Changed/racy sources validate their complete-line prefix before reuse. Replacement, truncation, rewrite, partial final row, read failure and clock reversal cannot certify stale input evidence. Public lists are independent copies; record changes that affect queues/consumption/skip diagnostics invalidate attribution reuse.

The service's status publisher retains parsed delivery journals between collections. Each new collection opens the journal directory through the no-follow walk, lists its current names, and checks each regular file's device, inode, size, mtime and ctime. Only matching identities reuse parsed data; new or changed files take the guarded reader and validation again, and vanished names leave the cache. A read enters the cache only when both file times precede the collection's start by strictly more than the post cache's two-second timestamp tick; recent files are returned but read again next time, even if their stamp has since aged. Receipt existence is checked anew even for an unchanged journal. A directory replacement discards its earlier entries. Within one collection, journal inventories share the presentation's observation; returned proofs contain independent data. This cache applies only while publishing status: delivery, reconciliation, receipt confirmation and retirement retain their ordinary fresh journal reads. Native records and screens still supply the current alarm reading, and no complete alarm or Facts value is cached across collections.

### Program use and check outcomes

Detailed status lists each declared/retained harness with `working_now` true/false/null and `last_turn_ended` UTC/null. A positive working seat establishes true. False requires all relevant current seats known inactive; unknown/missing/currently absent evidence stays null. The reader uses exact saved joins without joining or registering. Last-use time needs one unambiguous ledger launch interval/harness; crossing/conflicting/missing intervals yield no time. Null never proves never-used. Routine status text includes no program-use subjects, bodies or seat activity summaries.

`runtime/proven/check-outcomes/<harness>.json` retains bounded latest_attempt and definitive events, with sequence, outcome, UTC completion, known installed version and safe fixed step/reason. Beginning a check keeps prior definitive evidence. Interrupted/unknown completion cannot clear a failure or invent a completion time. A completed same-program pass clears it; other programs do not. Short publication locks order completions by sequence even if clocks move and never span paid work.

Malformed/unreadable outcome evidence remains visibly unknown and is not overwritten. No assertion/native dialog text, exception, path, record or task enters it. Recognized account questions get fixed reasons; unknown questions get no guessed identity. A definitive failure names program, time and safe reason on every line, including overflow. Unknown attempts stay detailed. Older installations ignore these files and cannot show their alarms; they remain runtime evidence with no configuration authority.

Ship sessions retain 50,000 scrollback lines. Framework behavior is configured only through a declared ship-file field when needed; presentation caches and operational observations are never configuration.

The service log adds at most one `service-timing` summary per minute, after a duty or rest returns. It gives completed measurement counts, wall/CPU totals and maxima for status collections, deliveries, cross-ship post, reconciliation, passes and sleep. The status count and cost cover actual collection calls, including failed or refused calls; cheap scheduling checks have a separate numeric `status_checks` count and do not enter the collection timing. Pass time includes its duties, so those figures are not additive. A long duty belongs to the window in which it returns; summaries are observations, never scheduling or delivery evidence. The summary carries only numbers and fixed duty names.
