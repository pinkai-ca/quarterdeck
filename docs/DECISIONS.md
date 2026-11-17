# Design decisions

Quarterdeck runs terminal coding agents under one human's command. It launches
programs, carries messages, reads their state and accounts for their work. The
human still decides what work matters, who reviews it and when changes merge. These
choices keep that control plane understandable when a program changes, a
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

A program's quirks stay in its adapter. OpenCode keeps every session of a data
directory in one database, so the OpenCode adapter, in
[src/quarterdeck/harness/opencode.py](../src/quarterdeck/harness/opencode.py),
gives each launch a separate data directory, and the join to one record stays
exact. OpenCode interrupts a turn on Escape pressed twice, so the same adapter
names that sequence as the interrupt key, and the door presses every key an
adapter names.

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

The one typing process is the ship's service. A `qd` command never types into a
working seat itself: the command files a request, and the service types. The service
writes each delivery attempt before the door types the message, and the
attempt's result after.

The door types each message in full, under a header line and over a footer
line. The pane is where the person reads, by scrolling up, and a file name there
tells a person nothing. Quarterdeck fixes no window size: the door compares the
paste with the box as the program wraps the text at the pane's present width. A
pane scrolled up still takes a delivery. The scrolled-pane test,
`test_a_scrolled_pane_is_delivered_through` in the same test file as the
foreign-draft test, delivers into a pane in copy mode and requires the delivery
confirmed and the reader's place kept.

A message for a working seat is typed on arrival where the program queues
input, and waits for the turn's end where the program does not. A sender who
needs the message read sooner chooses a faster speed: `qd post --now` has the
model read the message at the model's next step, and `qd post --interrupt`
stops the turn first, where the program has those keys.

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

A token class that the provider never reports is a known zero, not an
unmeasured one. A turn whose record states no completion ends at its last step,
with the turn's duration unknown. Both rules live in
[src/quarterdeck/harness/types.py](../src/quarterdeck/harness/types.py). A
JSON-lines reader, in
[src/quarterdeck/harness/json_record.py](../src/quarterdeck/harness/json_record.py),
skips a line the reader cannot parse, such as a half-written last line or a
shape from a newer version, so a program's update never blinds a seat. A record
file that cannot be read at all is an error, and the reading says so.

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
used by `qd laws`. `laws`, in the same file, reads
[standing-orders/laws.md](../standing-orders/laws.md), which groups the laws by
the part that enforces each law. The repository guard,
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

## `qd` is written for an agent at the keyboard

The person speaks to the desk, a seat like any other, and the desk runs the ship
with `qd`. An agent acts on what a command prints, so every answer of `qd` says
what happened, for an agent as much as for a person.

`qd` has one verb per act. Every act prints a proof line that says what was done
and how it was confirmed. Every reading is available as JSON, and every refusal
names its reason and its remedy. The desk's card,
[standing-orders/roles/desk.md](../standing-orders/roles/desk.md), tells the
desk to read the proof line before saying a thing was done.
[src/quarterdeck/cli.py](../src/quarterdeck/cli.py) holds the verbs, and
[src/quarterdeck/output.py](../src/quarterdeck/output.py) renders each reading
for a person or serializes the reading for a caller. The proof-line test,
`test_set_writes_the_ship_file_and_says_what_applies_it` in
[tests/unit/test_cli.py](../tests/unit/test_cli.py), sets keys on a seat that is
down, and requires a proof line for each write that names the act applying the
change.

## The service is one process per ship, not a seat

Two writers of one file race, as two typists into one box do. Each ship
therefore runs one service: a plain process, run by whoever runs `qd up`, that
outlives the shell it started from. An exclusive lock, held for the service's
whole life, is the one fact of whether the service exists. The heartbeat says
how recently the service ticked, and a busy service, which holds the lock, never
reads as dead. The service is therefore known by the holder of its lock, the
process the kernel lists as holding it (`servicectl.holder`, two readings of
`/proc/locks` agreeing), never by the pid a beat names: a killed service's last
beat outlives it, and after a sync back to an earlier copy the beat a reader finds
may name a process that is no longer the service. A beat counts as a start only
when its pid is that holder, and `qd stop` signals the holder alone. Inside a
jail, which sees no lock, the beat stands alone. A reader never takes the lock to
learn whether it is held: it reads the kernel's table by device and inode, since
a reader taking the lock for an instant makes a service starting that instant
refuse it. The kernel serves that table a page per read, so one reading can pass
over a held lock's row: with eight processes each taking and releasing fifty
locks, a lock held throughout was missing from 1,948 of 77,138 readings, in runs
of up to three readings back to back. A reading with no row is therefore
confirmed from the descriptors that hold the file open (their fdinfo) before the
lock reads as free, and where those cannot be listed the reading is unknown,
never free. `lock_holders` is that one reader, for the service's lock and the
installer's. Only where
the table cannot say (it cannot be read, or it may leave the holder out, as in a
nested pid namespace) is the lock probed by taking it. A
row whose pid is 0 or less names no holder, and a start keeps trying a held lock
for a moment (`TAKE_SECONDS`) before it refuses, since a reader of an earlier
copy still probes. The framework installs no system unit for the service, because
`qd` writes only inside its project folder (law `one-folder`).

The service alone writes the Ledger, and every reading opens the Ledger
read-only, so a status, a watch or a ledger table never races the writer.
[src/quarterdeck/servicectl.py](../src/quarterdeck/servicectl.py) holds the
lock, the heartbeat and the requests, and
[src/quarterdeck/ledger.py](../src/quarterdeck/ledger.py) opens the book
read-only for a reading. The reading test, `test_no_reading_creates_a_ledger` in
[tests/unit/test_json_readings.py](../tests/unit/test_json_readings.py), runs
status, the status line, watch, ledger and inbox on a ship with no ledger yet,
and requires that the readings create none.

## One project folder holds the framework and its ships

A ship's declaration belongs in a commit, and a ship's runtime never does: the
runtime holds ids, records, credentials and usage (law `nothing-leaks`). Both
sit in the one project folder that `qd` writes in. The framework's checkout
holds the ship folders, and each ship folder holds the ship's declaration. When
a ship folder is a repository, `qd set` commits each change it writes there.
Each ship's `runtime/` is a nested repository with no commits: a fence that
keeps a Git command run in a containing repository out of the runtime.

`fence`, in [src/quarterdeck/ship.py](../src/quarterdeck/ship.py), makes the
runtime a nested repository and adds `runtime/` to the ship's ignore file. The
fence test, `test_fence_makes_the_runtime_a_nested_repository` in
[tests/unit/test_ship.py](../tests/unit/test_ship.py), runs `git clean -fdx` and
`git reset --hard` in the ship folder and requires the runtime's files to
remain.

## Every seat reads one brief, built from files the ship owns

Crews differ in how they review and land work, and Quarterdeck enforces none of
their rules. The default ship, the smallest crew on real programs, has a desk, a
captain and an engineer; its orders have the captain review a change once and
the desk land it. Another ship writes other orders.

Every seat reads one brief at boot: the laws, the ship's orders, the card for
its role, the card for its harness and a section on its own seat. The laws are
the framework's, and the orders are the ship's own `orders.md`. A ship replaces
any card of the framework's with its own at the same path in the ship folder,
so the framework's cards are defaults. No instruction file of the person's
enters a brief. `qd brief <seat>` prints exactly what a seat reads, and
`--sources` names the file behind each part. When a file behind a brief
changes, the service renders the brief again and tells each running seat whose
brief changed.

The desk writes the person's word as a rule, in the desk's own words, in the
file that owns the rule: the ship's orders, a role card or the ship file. The
desk never records the person's words themselves. A rule in a file reaches every
seat that reads the file, and survives a fresh start.

`brief` and `card`, in
[src/quarterdeck/standing.py](../src/quarterdeck/standing.py), build the brief,
and `Service.rebrief`, in
[src/quarterdeck/service.py](../src/quarterdeck/service.py), renders the brief
again. The rebrief test,
`test_changed_orders_rebrief_every_seat_with_a_message_from_ship` in
[tests/integration/test_service.py](../tests/integration/test_service.py),
rewrites a ship's orders while a seat runs, and requires the new orders in the
seat's brief and a notice from `ship` delivered to the seat.

## A seat's home holds the ship's files and nothing of the person's

A program keeps its settings, instruction files, transcripts and logs under one
home folder. A seat that ran in the person's home would follow the person's
instruction files and write its sessions among the person's own. Each seat
therefore gets a home of its own, built from the ship's files. Where a program
takes its home from a switch, a ship session stays apart from the person's
sessions in logs and instruction files alike.

Each adapter hands its program the brief by the program's own switch. Claude
Code reads only the settings of its seat home and takes the brief as an
appended system prompt. Codex takes the brief as developer instructions and
reads no project document. pi takes the brief as an appended system prompt and
loads no context files. OpenCode takes the brief as its one instruction file.
Grok takes the brief as its rules, runs in a private home and trusts the
workspace as its maker intends. Antigravity has no home switch: a seat passes
its settings on the command line and joins the one conversation that appears
after its launch, in the program's own folder under the person's home.

The home turns off the program's own memory in Claude Code, Codex and Grok,
because what must survive a conversation is a post or a file of the ship's. The
home also turns off the generated prompt suggestions of Claude Code and Grok.

`build_home` and `launch`, in each adapter under
[src/quarterdeck/harness/](../src/quarterdeck/harness/), build the home and the
command line, and
[src/quarterdeck/harness/home.py](../src/quarterdeck/harness/home.py) layers the
ship's settings over the program's own. The home test,
`test_the_launch_carries_our_files_and_nothing_of_the_users` in
[tests/contract/test_claude.py](../tests/contract/test_claude.py), builds a
Claude Code seat's home and launch, and requires memory and suggestions off, the
seat's own keys passed through, and a launch that reads only the home's
settings.

## A seat shares its pool's credential by one link

A subscription's refresh token is good for one use, so a copy of a credential
dies at the first refresh. A seat's home therefore links to the pool's
credential file, and every seat on the pool shares the one live file. A program
that refreshes by atomic rename turns the seat's link into a private file that
holds the only live token. On every tick the service finds such a file, puts the
newest live one in the pool's place, keeps a copy of the replaced file under
the runtime and links the seat again. A tick alone is too slow, because a
renewal retires the token that every other seat on the pool still holds. The
service therefore also looks at each linked seat's credential twice a second,
and acts at once when a link has become a file. A watch that keeps failing logs each
cause once, by its kind and a fixed reason and never its message, and logs again once it
runs clean; a round skipped on the lock is not a clean one. A temporary seat's home is
removed only after its reseed: when that reseed is skipped because another holds the lock
and the home of a seat that may give its pool a sign-in (never a jailed one, nor one on an
endpoint pool) still holds a detached credential, the home is kept, the status line names it as a renewal held in a retired seat's home,
and the service's next pass reseeds the pool from it and then removes it. A program that writes its refresh through
the link would race other seats in the pool's own file, so the seats of that
program hold a copy instead, brought up to the pool's file on the tick. Putting
a refreshed file in the pool's place is the one write `qd` makes outside its
project folder (law `one-folder`). A key pool shares no file: the key reaches
the program by an environment variable. The status line warns ahead of a
grant's expiry.

[src/quarterdeck/credential.py](../src/quarterdeck/credential.py) reseeds the
pool and relinks the seats, and `credential`, in
[src/quarterdeck/harness/home.py](../src/quarterdeck/harness/home.py), brings a
pool's credential into a home. The relink test,
`test_a_refreshed_seat_reseeds_the_pool_and_is_relinked` in
[tests/unit/test_credential.py](../tests/unit/test_credential.py), replaces a
seat's link with a refreshed file, and requires the pool to hold the new token,
the seat linked again and the replaced file kept under the runtime.

## A seat runs without approval prompts, and a seat on an endpoint pool runs in a jail

Every seat runs `qd post` from its launch (law `can-post`), and the seat's
orders say what the seat may change. Each adapter therefore starts its program
without approval prompts. A ship that wants a sandbox asks for one seat by seat,
with `sandbox = true`.

A model served locally is trusted less than the subscription programs, so a
seat on an endpoint pool runs in a bubblewrap jail unless the ship says
`sandbox = false`. In the jail the root is read-only, the person's home is an
empty one, the workspace and the seat's own folder are writable, and the other
seats' folders are hidden. A jail runs as the person's own user, so whatever of
the post a jailed seat could see, the seat could read. The seat is therefore
shown a view of the post that the service keeps for it: a copy of each message
the seat sent or received, and the seat's own inbox. One folder of the post is
writable inside, the seat's own drop, so `qd post` works from inside: the seat
writes a message there, and the service files the message. Of the ship's
runtime the seat sees its own brief, the ship's `qd` and the service's
heartbeat, and nothing of the Ledger, the logs or the sign-ins. The person's
files, sign-ins and the other seats' records are absent from the jail.

[src/quarterdeck/sandbox.py](../src/quarterdeck/sandbox.py) builds the jail,
[src/quarterdeck/post_view.py](../src/quarterdeck/post_view.py) keeps each
jailed seat's view of the post, and `Ship.jailed`, in
[src/quarterdeck/ship.py](../src/quarterdeck/ship.py), decides which seats run
in one. The jail test,
`test_a_jailed_seat_writes_its_workspace_posts_from_inside_and_cannot_see_the_operators_home`
in [tests/integration/test_sandbox.py](../tests/integration/test_sandbox.py),
jails a seat, has the seat write in its workspace, read a file in the person's
home and post a report, and requires the write and the report to land and the
person's file to stay unseen.

## A control is written to the ship file, then typed as the program's own command

A control the person sets through the desk outlasts down, up and restart.
`qd set` writes the ship file first, and commits the change when the ship folder
is a repository. `qd set` then hands the change to the service, which types the
control's in-session command, such as `/model`, through the door, and confirms
the change by the program's resulting state. The command then stands in the
pane and in the session log, where the person reads the change. A control with
only a launch form is written and reported with the relaunch that applies the
control, never applied quietly. Into a session the human owns, the ship types no
control: the answer names the relaunch, or the command for the person to type.

A command typed by hand in a pane changes the program and not the ship file.
The ship file says what should be, the Watch says what is, and `qd status` marks
any difference. `qd set <seat> --adopt` writes what a seat runs into the ship
file. Keys the framework does not read pass to the program verbatim, from
`[seats.<name>.settings]`, so a new key of a program needs no change to the
program's adapter.

`set_seat` and `apply_control`, in
[src/quarterdeck/cli.py](../src/quarterdeck/cli.py), write the change and hand
the change on, and `set_values`, in
[src/quarterdeck/ship.py](../src/quarterdeck/ship.py), writes and commits
several keys as one edit. The control test,
`test_controls_go_through_the_service_by_slash_command` in
[tests/integration/test_service.py](../tests/integration/test_service.py), sets
model, pool and effort on a running seat, and requires the file written, the
model and effort applied by slash command and confirmed by the screen, the pool
left for a restart, and the change shown in the pane.

## A fresh start is a relaunch, never a typed clear

A typed clear cannot change a seat's pool or program, leaves a second record in
the seat's home, and cannot refresh a brief passed at launch. Restart and fresh
therefore share one relaunch path and differ only in resume. `qd restart <seat>`
relaunches with resume: the conversation continues in a new process.
`qd fresh <seat>` relaunches without resume: a new conversation begins with the
brief and the open messages. Nothing is typed to clear a seat.

`fresh = "after-order"` starts a seat fresh each time the seat's last open order
closes and the closing turn ends, so every order begins on a clean context. The
setting suits a worker, which takes one order at a time. A command seat is
long-lived and keeps its context.

`Seat.restart` and `Seat.fresh`, in
[src/quarterdeck/seat.py](../src/quarterdeck/seat.py), share the down and the
launch, and differ in resume alone. The relaunch test,
`test_restart_resumes_the_record_and_fresh_does_not` in
[tests/integration/test_seat.py](../tests/integration/test_seat.py), restarts a
seat with an open order and requires the same record with the order indexed,
then starts the seat fresh and requires a new record, the open thread indexed
again and the old record kept for the Ledger.

## Programs are found on the PATH and proven, never pinned

Programs update under a running ship. A seat runs the program found on the PATH
that `qd` runs with, and Quarterdeck pins no path and no hash. An unproven
version runs, and `qd doctor` names the version with the `qd check` that proves
the version.

`qd check <harness>` proves the installed version live, on a throwaway seat. The
check pastes a message long enough to fold, matches the paste as the program
shows it, finds the message in the record, and asks the program for one task
the check verifies without reading prose: write a named file with a given line,
and post a report on the order. When everything holds, the check marks the
version proven in the runtime. The check spends real tokens, so the check runs
only when asked. The drill runs on the drydock's simulator, and on real
programs only when a real ship is named.

[src/quarterdeck/check.py](../src/quarterdeck/check.py) runs the check. The
check test, `test_the_check_proves_its_seat_ran_the_selected_program` in
[tests/unit/test_check_selected_program.py](../tests/unit/test_check_selected_program.py),
requires the proven mark to name the program the seat ran, and no mark when the
seat ran another program.

## `qd doctor` hands what it finds to a seat

A script names a fault; a program that runs the ship reads the fault and mends
the fault. `qd doctor` checks the installation and the declaration, with a proof
line per check: tmux and git, each harness's program and its version, each
pool's sign-in and when that sign-in's grant expires, every seat's working
directory, the runtime's fence, the service and the disk. With `--fix`, what the
checks find goes as an order to a throwaway seat on the first pool whose program
runs. The order asks the seat to fix what can be fixed from the machine and to
report, and the checks then run again. A fixing seat spends tokens, so the seat
opens only when asked.

[src/quarterdeck/doctor.py](../src/quarterdeck/doctor.py) runs the checks. The
fix test, `test_the_seats_report_is_what_the_doctor_waits_for` in
[tests/integration/test_doctor.py](../tests/integration/test_doctor.py), hands a
finding to a throwaway seat, has the seat report, and requires the report as
the doctor's result, with no session left open.

## A pool names a way of paying, never the secret itself

A provider is paid by subscription, by API key or through an endpoint. A pool is
one way of paying, and a ship declares a pool for each way the ship pays, with
any number of pools for one provider. A subscription pool names the folder of a
sign-in. A key pool names its key as `env:NAME` or `file:PATH`; the ship file
never holds the key, and the key reaches the program by an environment
variable. A key pool is measured by spend, and an optional budget per day, week
or month is its wall.

Until the routed key isolation lands, no seat launches on a pool paid by a key,
a key pool or a keyed endpoint, jailed or not: the key would reach the seat's
program by its environment, and so the model's tool commands. Every launch door
refuses it with a line naming that change; the ship file still loads, so a seat
already running on a key can be seen and stopped.

An endpoint pool is a model served at an OpenAI-compatible URL, local or
remote, with no credential of its own and no quota wall. The machine that runs a
local server pays for that server, and a key named beside the URL pays for a
remote one. A pool names one URL, so each server is a pool of its own. When a
seat's home is built, Quarterdeck reads the endpoint's own listing of the
models the endpoint serves, with the context length where the server states
one, and the ship file does not repeat the listing.

`Pool`, in [src/quarterdeck/ship.py](../src/quarterdeck/ship.py), holds the
kinds, and [src/quarterdeck/endpoint.py](../src/quarterdeck/endpoint.py) asks an
endpoint what the endpoint serves. The key test,
`test_keyed_discovery_doctor_and_adapter_homes_never_store_the_key` in
[tests/unit/test_endpoint_auth.py](../tests/unit/test_endpoint_auth.py), names
an endpoint's key by environment and by file, builds OpenCode and pi homes on
the pool, and requires the key in each launch's environment and in no file of
either home.

## At a wall a seat moves to its next pool and stays there

A subscription's allowance resets on the subscription's own clock. A seat that
went back to its first pool at every reset would leave a later pool's allowance
unspent. A seat's `pool` is therefore one name or an ordered list. At a wall,
once the seat's record shows the seat idle, the service moves the seat to the
first later choice known to have room, then to an earlier one. The seat stays on
the new pool until that pool walls too, even when an earlier pool resets, so
each subscription is spent to the full. The move resumes the conversation when
the destination runs the same program, and starts fresh when the destination
runs another. A seat with no choice left stays at its wall, and the status line
shows the seat held.

`Service.failover`, in
[src/quarterdeck/service.py](../src/quarterdeck/service.py), makes the move. The
failover test, `test_a_seat_moves_on_at_a_wall_and_stays` in
[tests/integration/test_service.py](../tests/integration/test_service.py), walls
a seat's first pool, and requires the seat relaunched on the second, a notice
from `ship` that names the move, and the seat still on the second pool after a
further service pass.

## The Ledger keeps each turn's tokens, tools and machine use

Tokens say what a seat cost the provider, and machine figures say what the seat
cost the shared machine. The Ledger keeps both, turn by turn, from each
harness's own record, charged by pool, by seat and by order (law
`every-turn-accounted`). A turn's row holds the tokens in the provider's
classes, the tool calls, the turn's duration, and the processor seconds, peak
memory, most processes at once and bytes written to disk of the seat's process
tree. A turn read twice counts once.

[src/quarterdeck/ledger.py](../src/quarterdeck/ledger.py) keeps the book, and
[src/quarterdeck/machine.py](../src/quarterdeck/machine.py) samples each seat's
process tree on the service's tick. The accounting test,
`test_every_turn_is_accounted_by_pool_seat_and_order` in
[tests/integration/test_service.py](../tests/integration/test_service.py), has a
seat close an order with one tool call, and requires one turn for the seat, with
the turn's tokens and tool call, charged to the order the turn closes and to the
seat's pool.

## The status line is the alarm that passes through no door

An alarm typed into a pane would need the door, and the door holds while a box
is not clear. The status line of every ship session needs no door. Tmux draws
the line and refreshes the line itself, by running `qd status --line`, so a dead
service never looks like a healthy idle ship. While the service is running, the
command reads its valid published line. With an ok service lead, a publication
written no more than the refresh interval plus ten seconds ago reads plain
(forty seconds by default), with its collection-start timestamp retained.
Unchanged inputs refresh thirty seconds after the last collection finishes;
recorded input changes request a reading at the next cheap check. The ship file
may set `[ship] status_refresh` from five through three hundred seconds. An older
publication, or a busy service, reads stale with its write age through three
times that interval (ninety seconds by default). Beyond that bound the command
collects current facts itself and says `publication <age>s old, collected here`
in the lead. It also
collects the facts itself when the service is down, nothing valid is published,
or the publication's write age cannot be known. Thus a stopped publisher cannot
hide a newly waiting alert behind its last line. The line names every withheld or
blind delivery, every seat that needs a human, every seat held at a limit with
nowhere to move, a filling disk, a grant about to expire and a dead service
(law `nothing-waits-silently`). A disk shows on the line as the filesystem that
holds the runtime, the workspace or the temporary folder fills. The line
carries operational alerts only; `qd status` and `qd ledger` show provider
allowances in detail.

[src/quarterdeck/watch.py](../src/quarterdeck/watch.py) builds the line, and
`status_line`, in [src/quarterdeck/tmux.py](../src/quarterdeck/tmux.py), sets
the line on each session. The line test,
`test_the_status_line_says_everything_that_waits` in
[tests/unit/test_watch.py](../tests/unit/test_watch.py), requires a quiet ship
to read `service ok · 0 waiting` and a busy service to read busy, not dead. The
same test gives a dead service waiting and blind deliveries, a seat that needs a
human, a held seat, a full disk and a sign-in to renew, and requires one line
that counts each.

## Let the operating system schedule ordinary writes

Changed: ordinary framework and test-tool writes request no file or folder sync, and the ledger uses `synchronous=OFF`; the initial delivery attempt retains barriers for `one-door`, refusal quarantine and compaction transition records retain them for `context-is-cache`, and SQLite keeps its own hot-journal recovery syncs before OFF applies.
Risk accepted: power or operating-system failure can lose recent writes and can corrupt the SQLite ledger, requiring automatic reconstruction from the programs’ records; the original database stays as named evidence until its alarm is acknowledged.
Why rename stays: a writer closes its complete temporary before replacing the destination, so a reader and a process restart see a whole old or new file.
