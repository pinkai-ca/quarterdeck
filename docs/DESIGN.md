# Quarterdeck: design

## 1. What it is

Quarterdeck runs coding agents as visible terminal sessions under one person's command. Each agent is an ordinary terminal program in its own tmux window: the person can watch and type there. Quarterdeck launches the seats, carries messages, observes their work and accounts for provider and machine use. It is a control plane; the code agents write and the crew's review and landing policy belong to the ship's standing orders.

The ship's only configuration is `ship.toml`. Its declaration is committed in its own folder; runtime evidence stays ignored beside it. Agents may restart or lose context without losing their messages. Native records establish what they took and did, while screens establish whether typing is possible. An unreadable observation stays unknown.

## 2. The parts

Six parts operate the ship; standing orders describe its guarantees and crew rules, and the drill proves the public paths.

| Part | Responsibility | Entry points |
| --- | --- | --- |
| [Ship](design/ship-file.md) | Declares sessions, seats, workspaces, programs and paying pools; owns one service and runtime. | up, down, attach, status |
| [Harness](design/harnesses.md) | Adapts one program's launch, private home, records, screen, controls, sign-in and usage. | Called by the other parts; controls/check expose its capabilities. |
| [Seat](design/seats.md) | One program in one pane with launch/process/record identity, controls and lifecycle. | up/down/restart/fresh/stop for a seat; set |
| [Post](design/post.md) | Immutable orders, reports, questions, answers and acknowledgments with rendered inboxes and delivery evidence. | post, thread, inbox |
| [Watch](design/watch.md) | Reads activity from records and delivery readiness from screens; presents operational alerts. | watch, status --line |
| [Ledger](design/ledger.md) | Accounts for tokens, costs, tools, time, machine pressure and allowances by pool, seat and order. | ledger |
| [Standing orders](design/orders-and-laws.md) | Carries laws, ship orders and role/program cards to every seat. | brief, laws |
| [Drill](design/tests.md) | Exercises delivery, crash recovery and fresh starts using synthetic programs or explicitly requested real ones. | drill |

The [door](design/door.md) is the seat's one typing path, shared by delivery, controls, dialog answers and waking. [Structures](design/structures.md) combines seats/posts into forum reviews, cascade laps, a three-seat shuttle mission and the person's office. The [module list](MODULES.md) maps these parts to source files.

Antigravity scopes quota groups using the three exact model identities and group relations measured on agy 1.5.5. Its all-model group table is empty, and other relations remain unknown. The before-and-after usage proof is recorded in [Antigravity quota walls and usage](ANTIGRAVITY.md#quota-walls-and-usage).

`qd doctor` checks installation, programs, sign-in grants, working directories, runtime fences, service and disk, with proof lines per check. Its fix mode hands failures to a throwaway seat, waits for a report and checks again. It does not make a failed observation into a success.

## 3. What the human sees

Each declared session shows one window per seat and a status line independently refreshed by tmux. The line names operational problems: service down/busy/unknown, waiting or uncertain delivery, seats needing a person, spent seats without an eligible destination, disk pressure and expiring grants. Detailed status and ledger show provider allowances. Files hold the rest: TOML declarations, Markdown briefs/inboxes, immutable JSON messages, native records and SQLite accounting.

The person normally speaks to a desk seat. Its commands have one verb per act, structured JSON readings, proof lines saying what was observed, and refusals naming the reason and remedy. The desk's pane makes those acts visible. Crew messages arrive in full through the door; human-session seats receive through their own inbox watch. The guarded fixed waking line is permitted only when a message waits, no watch is armed and the person's box is empty.

A **turn** runs from one input through the program's work until it returns idle. A **step** is a reply, tool call or result inside it. Watch distinguishes open work from inactivity; Ledger sums the steps into turns and rollups.

### One message, from order to report

A seat files an order with post. The immutable file is the authority; the service renders the recipient's inbox and reserves a delivery attempt. A crew seat's door clears only recognized interruptions, pastes once into a known empty box, checks the displayed whole payload, submits and looks for the exact input in the owned record. A supported native queue can leave the order submitted and open until consumption. Unknown outcomes hold visibly rather than trigger unbounded retries. A human seat hears the same message through its watch.

The recipient reads the whole thread, acknowledges to claim the work, then acts. Acknowledgment does not close the order. The addressee's report referencing that order closes it; duplicate carriage preserves the same message identity and means act once. Fresh/restarted seats receive their brief and thread index, with their handoff on a fresh start. Accounting associates verified message evidence and turns without treating missing context as proof that no work occurred.

### Laws and crew rules

The canonical [laws](../standing-orders/laws.md) state only guarantees enforced in code and challenged by tests. Stable law IDs tag carriers and tests; `qd laws` connects them. They cover configuration, runtime separation, identity, one-door typing, record-first observation, durable messages and accounted turns. A conduct rule that code does not enforce belongs in ship orders or a role card. Quarterdeck carries those editable crew rules without enforcing their review or command policy.

## The pages

- [Ship file](design/ship-file.md), [harnesses](design/harnesses.md) and [seats](design/seats.md)
- [Door](design/door.md), [post](design/post.md) and [structures](design/structures.md)
- [Watch](design/watch.md) and [ledger](design/ledger.md)
- [Orders and laws](design/orders-and-laws.md) and [tests](design/tests.md)
- [Working on Quarterdeck](design/working.md) and [guided setup](design/guided-setup.md)
- [Modules and repository layout](MODULES.md#repository-layout)
- [Decisions](DECISIONS.md)

## Where the old sections went

Historical section citations map to these pages:

| Old section | Current page |
| --- | --- |
| 1. What it is | this overview |
| 2. The parts | this overview |
| 3. What the human sees | this overview |
| 4. The ship file | [Ship file](design/ship-file.md#4-the-ship-file) |
| 5. Harness adapters | [Harnesses](design/harnesses.md#5-harness-adapters) |
| 6. Seats | [Seats](design/seats.md#6-seats); typing moved to [Door](design/door.md) |
| 7. Post | [Post](design/post.md#7-post); reviews moved to [Structures](design/structures.md) |
| 8. Watch | [Watch](design/watch.md#8-watch) |
| 9. Ledger | [Ledger](design/ledger.md#9-ledger) |
| 10. Standing orders | [Orders and laws](design/orders-and-laws.md#10-standing-orders) |
| 11. The laws, second draft | [Laws](design/orders-and-laws.md#11-the-laws-second-draft) |
| 12. Tests | [Tests](design/tests.md#12-tests) |
| 13. Repository layout | [Modules](MODULES.md#repository-layout) |
| 14. Decisions | [Decisions](DECISIONS.md) |
| 15. Working on the framework from a ship | [Working](design/working.md#15-working-on-the-framework-from-a-ship) |
| Guided setup | [Guided setup](design/guided-setup.md#guided-setup); office/raise moved to [Structures](design/structures.md#office-and-raising-a-ship) |
