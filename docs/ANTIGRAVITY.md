# The Antigravity record

Antigravity (`agy`, record/screen contracts on 1.5.2 and 1.5.5) keeps every conversation as a SQLite database under its own state directory, `~/.gemini/antigravity-cli/conversations/<id>.db`, in WAL mode, with protobuf messages in its rows. The program has no home switch, so a seat runs in the user's Antigravity state and its record sits in a folder shared with the user's own conversations. This page says what the adapter reads from that record and how, so that a reader of the code or a reviewer of a fixture can check it against the program.

## Which record is the seat's

At launch the adapter notes the conversations already in the folder; the one that appears after the launch is the seat's, and two appearing at once is reported, never guessed (law `one-record`). Every record a seat has ever joined is listed in its home, in `conversations.owned`, and the program that wrote each is kept in the seat folder's reader registry, so a record stays the seat's across fresh starts and across a change of program, and is accounted by the reader that wrote it. A record the seat never joined is never opened, whoever's it is.

## How it is read

The record is never opened with SQLite in place: that can create the `-wal` and `-shm` files beside it, which is a write into the user's folder (law `one-folder`). The adapter reads the database file and its write-ahead log as bytes, checks that neither changed while it read them, applies the log's committed frames to the pages, and loads the result into an in-memory SQLite connection, where the tables are queried. Frames after the last commit, and frames from an earlier generation of the log, are left out, as the program itself would leave them. A file that is not a database, a log with a bad checksum, or a schema without the tables below is a `RecordError`, and the seat reads as unknown rather than as empty.

## The tables and the fields

Three tables carry a conversation: `steps`, one row per step with `idx`, `step_type`, `status`, `metadata` and `step_payload`; `gen_metadata`, one row per model generation; and `executor_metadata`, one row per execution that ended. The field numbers below were checked against the descriptors the 1.5.2 program embeds, and the tests check synthetic wire bytes against them.

| Message | Fields read |
|---|---|
| Step types (`steps.step_type`) | 14 user input, 15 planner response, 17 error message, 132 generic, a tool's result |
| Step statuses (`steps.status`) | active: 1 pending, 2 running, 8 generating, 9 waiting, 11 queued; done: 3 done, 4 invalid, 5 cleared, 7 error; aborted: 6 canceled, 12 interrupted |
| Step metadata | 1 created, 6 viewable, 7 finished generating, 8 completed, 32 started, all timestamps; 4 the tool call; 9 the model usage; 10 the model cost; 12 the execution id |
| User input, payload field 19 | 2 the text, 1 in older records; 12 the user's configuration, whose planner configuration names the model in field 28 |
| Planner response, payload field 20 | 8 the reply, 1 in older records; 7 the tool calls it made |
| Error message, payload field 24 | 3 the error details: 1 the message shown to the user, 3 the full error; a quota wall is read from these |
| Generic result, payload field 140 | 2 the result, whose field 6 is an `Any` naming the tool's own message; a run command carries its exit code and whether it timed out |
| Model usage, metadata field 9 | 1 the model as an enum; 2 input tokens, already less the cached ones; 3 output tokens, thinking included; 4 cache written; 5 cache read |
| Generation, `gen_metadata` | 1 the chat model, whose field 4 is the usage and 19 or 21 the model's name; 2 the steps it produced; 4 the execution id |
| Execution, `executor_metadata` | 1 why it ended: 1 error, 2 canceled, 3 to 11 the other ends; 3 its last step; 9 its id; 10 its configuration, naming the model |

## Turns, tokens, time

A turn begins at a user input and takes every step up to the end of its execution. It is closed by its execution's end, recorded in `executor_metadata` past its last step, or by a cancellation; a newer user input alone never closes the turn before it, and an input that arrives during an execution is a message taken into the running turn, not a new one. An error message appended after an execution ended is a trailer and reopens nothing. A status the adapter does not know leaves the turn's state unknown, and the record's turn count with it.

Tokens are summed over the turn's steps from their usage messages. Where a step has none and its generation made exactly one planner response, the generation's usage counts once, for that step, and never again for the tools listed beside it. A class the message does not carry is zero: this provider reports no cache writes. A step with no usage at all is counted as unmeasured, and every figure built on the turn carries a `+`. Cost is taken only where every step states it. Effort and subagent turns are not in the record and stay unknown.

A turn ends at its last step's completion time; where the record states none, the turn ends at its last step's time and its duration is unknown. Tool durations come from a step's start and completion; a tool interrupted or never completed has none. A tool failed when its status says so, its details carry an error, or its command exited nonzero or timed out; a result the adapter cannot judge is unknown, never a pass.

## Quota walls and usage

When the program hits its quota it writes an error message step whose text reads `Individual quota reached … Resets in …`, and shows the same banner on the screen. The record's step is the canonical event, and its creation time anchors the reset. The service persists the event once, by record and step, with the pool attributed to that event and the model its own step, execution or current turn establishes. An executor-error trailer keeps that execution's model; the record's later final model cannot place an earlier refusal. The refusal holds that exact observed model until its reset. If its model is unplaced, the refusal holds only its own still-unplaced observed source. It does not establish a quota group's membership or an all-model limit.

A banner seen on the screen before the record shows it is persisted under the launch it was seen on, with record-first model evidence and a reset estimated from the first observation and labelled so. The record's event supersedes the screen event when it appears. A banner the book already knows, even an expired one, is not a new wall. Event scope lives in a side table, `wall_scopes`, bound to the retained event tuple; the old positional `wall_events` layout remains unchanged. An older book without scope evidence leaves Google scope unknown. Downgrading to an older writer restores its former whole-pool decisions while that code runs.

Usage comes from the program's own command, `agy -p /usage --output-format json`, which on 1.5.2 answers without opening a conversation. The service runs it every five minutes for each declared Google subscription pool. Every returned group's bucket is retained independently and may constrain only proved member models. The historical Gemini 4 Argon weekly storage key stays `7d`; detailed status and ledger label its group, and show the other returned groups and periods separately. An unsuccessful whole read reports unavailable usage without inventing an Argon-only reading. The program is found on the PATH as every launch finds it.

`harness/antigravity_models.py` holds explicit membership, exclusion, canonical-identity and all-model facts. Its real fact tables are empty until reviewed statements from the program or its sign-in establish them. A group's label, a similar model name, a successful turn or a matching reset does not establish membership. Positive membership supplies no exclusion from another cap unless the native schema proves completeness or exclusivity. Unknown scope proves no group wall and no automatic destination room. With empty real tables a model can continue until its own provider refusal establishes its wall; no unknown group cap holds it preemptively.

The book keeps one complete reading per window, replacing fraction, reset and reading time together only with another complete reading. A failed or partial read changes only the latest-attempt metadata and leaves the retained measurement dated and labelled last known. A complete spent window remains a wall for its proved scope until its own reset or a newer complete reading with room, even through failed or stale reads. Automatic room requires current applicable windows, within the hour and before reset, with no unresolved relevant cap or refusal scope. Source decisions use the model observed in the running record, then screen; destinations use their own resolved model and provider policy. The door, routing, held-seat status and ledger share these decisions. The tmux line carries operational held reasons, without usage percentages.

`qd check antigravity --model <id>` selects the throwaway seat's model without editing pools or another seat. Requested-model confirmation uses the owned record first and the screen as a startup fallback, then requires a confirming owned-record model at completion. An absent screen model supplies no contrary evidence; an explicit screen model must confirm the same requested identity. Only exact equality or an adapter-proved native identity can confirm it. The proof names requested, launched and record-observed models. This source change supplies no new installed proof or proven mark.


## Version evidence

| Native version | Evidence |
|---|---|
| 1.5.2 | Existing record/schema, screen, fold and control contract fixtures remain supported |
| 1.5.5 | Live `qd check` passed on 11 November 2026 UTC (10 November, America/Vancouver), Gemini 3.8 Flash/medium, 220×50; separate opening and task turns, one task paste/submit, exact record receipt, file and post proof. New synthetic screen fixtures cover the native queue, edit hint, working/idle box and footer |

These are measured versions, not launch pins. Other versions keep the ordinary best-effort rule and are not called proven by these fixtures.

## Opening turn and queued input

`--prompt-interactive` makes the brief a native model turn. The check waits, within its 180-second startup bound, for a recognized open idle door and a record proving at least one ended turn before sending the task. An empty composer while the opening answer is working is not that readiness proof. The task must then have its own exact input receipt and an additional ended turn.

During ordinary work, 1.5.5 shows a queued envelope preview above the composer, an `alt+m to send immediately` hint, and `Press up to edit queued messages` below it. The record does not yet contain that queued input. The Door labels this **queued** only after matching the whole pasted box, submitting once into an unchanged launch, and observing an initially empty queue acquire one unique preview containing the complete transport kind/sender/short-id and a matching prefix, with the native edit hint and an empty recognized composer. Missing, foreign, ambiguous, already occupied or unreadable queue evidence supplies no such proof. A truncated body proves queue visibility only.

| Stage | Reading and evidence |
|---|---|
| Working before input | Open, queues; native record activity and screen are reported separately |
| Newly visible matching queue | `queued`, explicitly awaiting an exact record receipt; durable Proof stays `submitted` with optional `screen_queue: true` |
| Native record takes the original payload | Shared timestamped exact-input rule confirms delivery; the marker becomes false and the Proof/attempt becomes `delivered` |
| Queue disappears without a receipt | Pending; no second paste/submit. Missing record evidence never becomes delivery |
| Record stays unknown or the bound expires | The existing 600-second pending policy calls for inspection; launch/box uncertainty remains visible immediately |

A service restart reconciles the same payload, original timestamp and saved reader. Check transport uses the same Proof and, for a queued/pending submitted input, waits up to 180 seconds for exact consumption without resending. Task/report and ended-turn checks remain separate. A check that times out tears down its own seat and retains the abandoned, explicitly unconfirmed history; late receipts never authorize another submission. No record, token class, quota group or wall rule is rewritten.

`screen_queue` is an optional boolean in the existing private pending journal; old readers continue to hold its `submitted` phase. Existing public queue result values remain, but `queued` may now explicitly describe screen-only queue evidence; its detail and the pending journal distinguish that from a record receipt. Status/watch show the pending wait. Only the record still yields `delivered`.

## In brief, as the README stated it

This paragraph stood in the README until 12 November 2026. The sections above hold each of its facts in full.

Proven and not yet in daily use: Antigravity. In its check the opening brief and the check task were two separately read native turns. It has no home switch, so its seat runs in the program's own state; its record, a SQLite database of protobuf messages, is read from a snapshot of its bytes, so deliveries are confirmed and turns accounted like the others, and what the record does not hold is marked rather than guessed. A visible working queue waits for an exact record receipt before delivery is confirmed. The fields, version evidence and queue behaviour are in the sections above.
