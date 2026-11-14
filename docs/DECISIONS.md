# Design decisions

Quarterdeck runs terminal coding agents under one human's command. It launches
programs, carries messages, reads their state and accounts for their work. The
human still decides what work matters, who reviews it and when changes merge. These
five choices keep that control plane understandable when a program changes, a
message waits or evidence cannot be read. Each choice links to the code and to a
test of the failure mode it addresses.

## Six parts give responsibilities a home

The design uses six names for six responsibilities. They describe the machinery,
not a prescribed team structure:

| Part | Responsibility | Code |
|---|---|---|
| Ship | Declares sessions, seats, programs and their accounts or model endpoints in `ship.toml`. | [ship.py](../src/quarterdeck/ship.py) |
| Harness | Adapts one program's launch, record, screen and controls. | [harness/base.py](../src/quarterdeck/harness/base.py) |
| Seat | Joins a running program, its pane and its record under a role. | [seat.py](../src/quarterdeck/seat.py) |
| Post | Holds addressed orders, reports, questions, answers and acknowledgments. | [post.py](../src/quarterdeck/post.py) |
| Watch | Reads activity and delivery obstructions for a person to inspect. | [watch.py](../src/quarterdeck/watch.py) |
| Ledger | Accounts for turns, tools, time and cost from available evidence. | [ledger.py](../src/quarterdeck/ledger.py) |

Copying a program's special rules into delivery, accounting and status code would
give the same fact several owners. One copy could learn a new format while
another kept guessing from the old one. Program knowledge therefore belongs in
the harness; the other parts ask its interface. The adapter-boundary guard,
`test_production_program_knowledge_stays_in_adapters` in
[tests/unit/test_adapter_boundary.py](../tests/unit/test_adapter_boundary.py),
catches program names spreading into generic modules. It is a concrete check on
that separation, rather than proof supplied by the names themselves.

Standing orders and the Drill support these parts: one carries instructions, the
other exercises the control plane on simulated programs. Neither chooses a
project's review or merge policy. That remains an instruction from the human,
not a hidden consequence of the architecture.

## Everything typed goes through one door

An input box is shared with a person. Text already there might be an unfinished
thought, and an unfamiliar dialog might give the same key a different meaning.
Treating either as an inconvenience to clear would let delivery destroy work it
does not own.

Each seat therefore has one typing path, in one process: two typists into one
box would race and corrupt each other's text. `Door.deliver`, in
[src/quarterdeck/door.py](../src/quarterdeck/door.py), checks whether that path
is usable, clears only a recognized notice, pastes into an empty recognized
box, checks the displayed paste and submits it once. The foreign-draft test,
`test_text_in_the_box_is_an_obstruction_and_nothing_more_is_done` in
[tests/integration/test_door.py](../tests/integration/test_door.py), puts text
in the box, attempts delivery and requires the original text to remain
untouched, with nothing submitted.

A session owned by the human receives each message through the seat's watch,
not through keys. The door types one fixed line there, which tells the seat to
arm its watch, and only when a message waits, no watch is armed and the box is
empty, so that the person's own typing is never disturbed.

Submission is not confirmation. `Door.confirm`, in the same file as
`Door.deliver`, requires the exact payload in fresh record evidence. Queue
acceptance remains queued until consumption is established. Where the door
cannot confirm a delivery, `one_look`, in
[src/quarterdeck/pending.py](../src/quarterdeck/pending.py), looks once more,
record first, and settles three cases:

- The record shows the message: the message counts as delivered.
- The message's exact text still sits in the box: the door submits the text.
- The box is empty, the seat is idle and the record lacks the message: the door
  types the message once more, with the same id, under a line that marks the
  second delivery.

Anything else the door holds, and the door never types that message again by
itself. A held delivery shows in `qd watch` and on the status line. The tests
in [tests/unit/test_door_one_look.py](../tests/unit/test_door_one_look.py) take
each case in turn.

A message lost is worse than a message seen twice. The same id, and a line in
every seat's brief, make the two deliveries read as one message. Holding the
rest keeps the door from guessing.

An unfamiliar screen needs judgment, and no model's judgment sits inside the
typing path. The door types nothing at a screen the door does not know. Through
`qd`, such a screen is answered only by a choice that a systems seat makes
through the same path, in a seat no human owns, recorded before it is pressed.

## Read the record before interpreting the screen

A screen is useful for knowing whether typing is possible. It is weaker evidence
of what the program has accepted or finished: it can redraw, retain old text or
show an empty box while work continues. Treating its appearance as a complete
account would collapse several different questions into one guess.

`activity`, in [src/quarterdeck/watch.py](../src/quarterdeck/watch.py), reads
the program's record for turn state and reports `unknown` when that evidence
cannot be read or does not state the answer. A seat that has not started a
conversation has no record yet, and reads `idle, no record yet`. The door
separately reads the screen for an obstruction. Activity and permission to type can consequently
disagree without either reading being concealed.

The unreadable-record test,
`test_watch_and_seat_count_what_was_read_and_mark_what_was_not` in
[tests/unit/test_watch.py](../tests/unit/test_watch.py), makes a record
unreadable while retaining a recognizable screen. It requires unknown activity
and an incomplete token count, even though the screen can still indicate work.
The counting path, `tokens_today` in the same file as `activity`, preserves
that distinction: an unmeasured contribution is marked, not silently converted
into measured zero.
The same discipline keeps declared settings separate from observed settings.
An estimate or an unavailable measurement must remain recognizable as such;
a tidy display is not a reason to invent a fact.

## Messages are immutable; replies change what they mean

A report should add evidence about an order, not replace the order with a new
version. If receiving, acknowledging and completing work all changed one mutable
status field, a later reader could lose the distinction between what was asked,
what was claimed and what was actually reported.

Post keeps each message as an immutable file and uses explicit reply
relationships. `Store.write`, in
[src/quarterdeck/post.py](../src/quarterdeck/post.py), refuses to overwrite an
existing message. The immutable-message test,
`test_a_message_is_one_immutable_file` in
[tests/unit/test_post.py](../tests/unit/test_post.py), attempts that overwrite
and requires a refusal while the original remains readable. The inbox is a
reading of these messages, not their replacement.

Completion has its own rule. `Store.closing_report`, in the same file as
`Store.write`, looks for a report replying to the order from its addressee. The
closure test, `test_only_a_report_from_the_addressee_closes_an_order` in the
same test file, proves that an acknowledgment does not close the order and that
another sender's report cannot close it either.
This prevents apparent progress from silently becoming completion. Open work
can be offered again after a program starts fresh, without relying on what that
program remembers. The human can inspect both the original request and the
evidence that closed it.

## A law needs code and a test that challenges it

A written promise can outlive the behavior it describes. Calling it a law would
then give the reader confidence that the implementation has not earned.
Quarterdeck reserves that word for guarantees with code that carries them and
tests that attempt to violate them. Team conventions remain standing orders;
they are not promoted to framework guarantees merely because agents read them.

Tagged comments connect the promise to both places. `carriers`, in
[src/quarterdeck/standing.py](../src/quarterdeck/standing.py), builds the index
used by `qd laws`. The repository guard,
`test_every_law_is_carried_in_code_and_tried_by_a_test_and_every_tag_names_a_law`
in [tests/unit/test_repository.py](../tests/unit/test_repository.py), requires
every named law to have an implementation carrier and a test carrier, and
rejects tags naming no law. A comment-token test,
`test_only_comment_tokens_carry_laws_even_next_to_quoted_tags` in
[tests/unit/test_standing.py](../tests/unit/test_standing.py), also keeps quoted
example text from masquerading as an implementation tag.

The index makes a guarantee traceable; reading the negative test establishes
what it actually tries to prevent. Together they give a maintainer a practical
question before changing behavior: which promise changes, which code enforces
it, and which test would expose its loss?

## 14. Decisions

Decided with the first draft, 2026-11-06: the name; the MIT licence; the crew's names and roles; harnesses Claude Code and Codex first, Antigravity with them, Grok and Muse in the second round; landing and governance out of the framework, with the Captain and First Officer practice kept in the ship's orders; a minimal board with token tracking by provider, seat and order; the Desk as a managed seat; the accounting noun is the Ledger; pools named for their place with any number per provider; the laws traceable to the code that carries them; the Watch reduced to activity and door; "role" in place of "billet"; the flagship ship is "enterprise" and no hostname appears in the repository; the laws kept few by their admission test and grouped by the part that enforces them; the Engineering persona is Scott; workers sit in a third session named "Crew"; the live drill runs only on request; Standing orders and Drill are parts in their own right, the two that keep the six honest; a control changes by slash command where the program has one, because the change then stands in the session log, and a control that needs a restart is reported, never applied quietly; a fresh start is a first-class control; a program's own settings pass through as its own keys; a dialog the adapter knows is dismissed and one it does not is refused and reported; the human commands through the Desk and the Desk commands through `qd`, so `qd` is written for an agent at the keyboard; a control set through the Desk persists through down, up and restart, so `qd set` writes the ship file; the brief is Quarterdeck's own, independent of the user's instruction files, with every file behind it exposed in the ship folder; messages typed in full into the addressee's pane, because the transcript is what the human reads and a file name there tells a person nothing, settled by a probe showing both first-round programs render a sent paste in full; generated suggestions turned off at launch in every program; a seat's home holds nothing of the user's, so a ship session and the user's own are told apart in logs and instruction files alike; no links except the one credential, and only where the program takes no token by environment; versions proven, not pinned, an unproven version running and reported; API keys as pools beside subscriptions, named in the ship file and never written there, measured by spend with an optional budget as their wall; a home per seat built from the ship's files; orders and cards as files, the service re-briefing the seats they touch; no framework settings in the ship file.

Decided with the second draft, 2026-11-07, after a review of the first by four reviewers: an ack claims and only a report closes; fresh and restart as one relaunch path, the typed clear dropped; the service as the only process that types; a delivery attempt written before typing, a blind attempt shown and never resent by the service; text in the box an obstruction and nothing more, the still-draft lift cut; the status line as the alarm that passes through no door, and the law `nothing-waits-silently`; no fixed window size, reflow handled by comparison; a scrolled pane delivered through, settled by a probe; one path per seat, the Office human-owned and reached by its watch, the Bridge and the Crew by the door; one project folder, not one folder per ship, fenced by three nested repositories, with `qd set` committing what it writes, settled by a probe on git 2.53; delivery into a running turn at three speeds, settled by a probe; slash-command controls kept in the first release; four laws restated and `can-post` added; the fake harness built first and the week-one spike; the typed clear, the runtime path override, the per-seat outbox, the optional dependency and the "last woken for" attribution cut; tokens in as many classes as the provider bills; tool calls, durations, machine load and disk written in the Ledger beside the tokens, with the free space of every filesystem the ship writes to on the status line.

Decided during the build: a slash command sent by `qd set` updates the ship file, since `qd set` writes and commits the file before it types; a slash command typed by hand changes the program and not the file, and `qd set --adopt` writes what a seat is running into the file; versions are best effort and the join law is renamed `one-record`; `one-door` names the session the human owns; a ship's seats use the ship's instructions and nothing else, by each program's switch, settled for Claude Code by a probe; a seat's pool may be a list, the service moving the seat on at a wall by restart with resume and leaving it there until that pool walls too, never back on its own, so that two subscriptions are both spent to the full over a week; the fourteen laws of the second draft taken as they stand; the service is one process per ship, not a seat, run by whoever runs `qd up`, and nothing is pushed into systemd; `fresh = "after-order"` is for workers, command seats being long-lived; the live check and the live drill ask each program to do something and verify it did; commits carry no attribution trailers; credential handling shares by link, reseeds and relinks on the tick when a refresh breaks the link, and warns two days before a grant expires.

Decided after the first crew job, 2026-11-07, when a three-seat ship fixed its own Antigravity accounting and the review of that work came back: a seat runs its program without a sandbox and without approval prompts, and a ship that wants a sandbox asks for it seat by seat; a program's own memory is off in a seat's home; the operator's word becomes the ship's through the desk, written as a rule in the ship's files and never kept as a quotation; standing orders are per ship, the framework's cards being the defaults; the smallest crew on real programs is the default ship, a desk, a captain and an engineer, where the captain reviews once and the desk lands; the fixture ship is the drydock and its captain is Picard; a program is found on the PATH or named in the ship file, proven live and never pinned by path or hash; what a record did not hold is counted and marked with a `+`, a class the provider never reports is zero, and a turn without a completion time ends at its last step; a reading never writes the Ledger; a record line the reader does not know is skipped; file size is guidance and the test enforcing it is gone; at that stage every subscription pool's headline usage was on the status line, Google's by its own usage command every five minutes; `qd doctor` checks the installation and hands what it cannot fix to a seat; and working on the framework from a running ship is a design concern of its own, [section 15](design/working.md#15-working-on-the-framework-from-a-ship). Open: the installed copy of [section 15](design/working.md#15-working-on-the-framework-from-a-ship).

Decided on the evening of 7 November 2026, for the local models: a third pool kind, the endpoint, a model served at an OpenAI-compatible URL with no credential and no quota wall, a key beside it where a remote service wants one, reaching the program by environment variable and never landing in a file; one pool per server, so a machine with a model on each of two cards declares two pools and a seat sits on one; a seat on an endpoint pool jailed with bubblewrap unless the ship says otherwise, and any seat jailed on request, because a local model is trusted less than the subscription programs; OpenCode and pi as the fourth and fifth adapters, both on endpoint pools, their records read like the others' (OpenCode's SQLite from a snapshot shared with Antigravity's reader, pi's JSON lines), their briefs by the program's own switch (an instruction file, the appended system prompt), their folds and wraps pinned by fixtures; the model a seat runs read from the endpoint's own listing at launch, with its context length where the server states one; a program that keeps every session in one database given a data directory per launch; a program that wants its interrupt key twice naming the sequence in its keys; and a program's word wrap allowed a slack of columns where it wraps short of the edge.

## Let the operating system schedule ordinary writes

Changed: ordinary framework and test-tool writes request no file or folder sync, and the ledger uses `synchronous=OFF`; only refusal quarantine and compaction transition records retain barriers for `context-is-cache`.
Risk accepted: power or operating-system failure can lose recent writes and can corrupt the SQLite ledger, requiring reconstruction.
Why rename stays: a writer closes its complete temporary before replacing the destination, so a reader and a process restart see a whole old or new file.
