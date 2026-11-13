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
cannot be read or does not state the answer. The door separately reads the
screen for an obstruction. Activity and permission to type can consequently
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
