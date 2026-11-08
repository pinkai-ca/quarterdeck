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
| Harness | Adapts one program's launch, record, screen and controls. | [harness/base.py](../src/quarterdeck/harness/base.py#L70) |
| Seat | Joins a running program, its pane and its record under a role. | [seat.py](../src/quarterdeck/seat.py) |
| Post | Holds addressed orders, reports, questions, answers and acknowledgments. | [post.py](../src/quarterdeck/post.py) |
| Watch | Reads activity and delivery obstructions for a person to inspect. | [watch.py](../src/quarterdeck/watch.py) |
| Ledger | Accounts for turns, tools, time and cost from available evidence. | [ledger.py](../src/quarterdeck/ledger.py) |

Copying a program's special rules into delivery, accounting and status code would
give the same fact several owners. One copy could learn a new format while
another kept guessing from the old one. Program knowledge therefore belongs in
the harness; the other parts ask its interface. The
[adapter-boundary guard](../tests/unit/test_adapter_boundary.py#L65) catches
program names spreading into generic modules. It is a concrete check on that
separation, rather than proof supplied by the names themselves.

Standing orders and the Drill support these parts: one carries instructions, the
other exercises the control plane on simulated programs. Neither chooses a
project's review or merge policy. That remains an instruction from the human,
not a hidden consequence of the architecture.

## Everything typed goes through one door

An input box is shared with a person. Text already there might be an unfinished
thought, and an unfamiliar dialog might give the same key a different meaning.
Treating either as an inconvenience to clear would let delivery destroy work it
does not own.

Each seat therefore has one typing path. [Door.deliver](../src/quarterdeck/door.py#L150)
checks whether that path is usable, clears only a recognized notice, pastes into
an empty recognized box, checks the displayed paste and submits it once. The
[foreign-draft test](../tests/integration/test_door.py#L89) puts text in the box,
attempts delivery and requires the original text to remain untouched, with
nothing submitted. A session owned by the human receives messages through its
watch instead of having keys typed into it.

Submission is not confirmation. [Door.confirm](../src/quarterdeck/door.py#L235)
requires the exact payload in fresh record evidence. Queue acceptance remains
queued until consumption is established. An uncertain submission stays visible;
ordinary retries do not resend it. This spends convenience to preserve the
person's control: inspection and an explicit recovery decision are preferable
to a second command whose first outcome is unknown.

## Read the record before interpreting the screen

A screen is useful for knowing whether typing is possible. It is weaker evidence
of what the program has accepted or finished: it can redraw, retain old text or
show an empty box while work continues. Treating its appearance as a complete
account would collapse several different questions into one guess.

[Watch.activity](../src/quarterdeck/watch.py#L46) reads the program's record for
turn state and reports `unknown` when that evidence cannot be read or does not
state the answer. The door separately reads the screen for an obstruction.
Activity and permission to type can consequently disagree without either reading
being concealed.

The [unreadable-record test](../tests/unit/test_watch.py#L41) makes a record
unreadable while retaining a recognizable screen. It requires unknown activity
and an incomplete token count, even though the screen can still indicate work.
[The counting path](../src/quarterdeck/watch.py#L130) preserves that distinction:
an unmeasured contribution is marked, not silently converted into measured zero.
The same discipline keeps declared settings separate from observed settings.
An estimate or an unavailable measurement must remain recognizable as such;
a tidy display is not a reason to invent a fact.

## Messages are immutable; replies change what they mean

A report should add evidence about an order, not replace the order with a new
version. If receiving, acknowledging and completing work all changed one mutable
status field, a later reader could lose the distinction between what was asked,
what was claimed and what was actually reported.

Post keeps each message as an immutable file and uses explicit reply
relationships. [Store.write](../src/quarterdeck/post.py#L100) refuses to overwrite
an existing message. The [immutable-message test](../tests/unit/test_post.py#L16)
attempts that overwrite and requires a refusal while the original remains
readable. The inbox is a reading of these messages, not their replacement.

Completion has its own rule. [Store.closing_report](../src/quarterdeck/post.py#L144)
looks for a report replying to the order from its addressee. The
[closure test](../tests/unit/test_post.py#L30) proves that an acknowledgment does
not close the order and that another sender's report cannot close it either.
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

Tagged comments connect the promise to both places.
[standing.carriers](../src/quarterdeck/standing.py#L125) builds the index used by
`qd laws`. The [repository guard](../tests/unit/test_repository.py#L41) requires
every named law to have an implementation carrier and a test carrier, and rejects
tags naming no law. A [comment-token test](../tests/unit/test_standing.py#L61)
also keeps quoted example text from masquerading as an implementation tag.

The index makes a guarantee traceable; reading the negative test establishes
what it actually tries to prevent. Together they give a maintainer a practical
question before changing behavior: which promise changes, which code enforces
it, and which test would expose its loss?
